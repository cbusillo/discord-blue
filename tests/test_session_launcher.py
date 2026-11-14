from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import discord

from discord_blue.codex_bridge.bridge import CodexBridge
from discord_blue.codex_bridge.config import BridgeConfig, socket_for_home
from discord_blue.codex_bridge.launcher import (
    STARTED_IN_A_TERMINAL,
    Command,
    CommandResult,
    LiveBridges,
    LaunchOutcome,
    MacLauncher,
    claude_environment,
    find_repositories,
    run_command,
)
from discord_blue.doodads.agent_session.launcher import BUSY_CLOSE_CODE, NOT_CONNECTED
from tests.discord_client import BYSTANDER, OPERATOR, TOKEN, RunningCog, app_command, running_cog
from tests.fake_discord import FakeDiscord
from tests.test_attach_scenarios import until
from tests.fakes_agent_prompts import prompt_fixture
from tests.fakes_agent_session import FakeWebSocket
from tests.test_codex_bridge import FakeRpc, running_bridge

Json = dict[str, Any]
CLAUDE_OUTPUT = "backgrounded · 4622a28e · alpha (idle — send a prompt to start)\nmore detail\n"
CLAUDE_SESSION_ID = "4622a28e-0000-4000-8000-000000000000"
NO_JOB = CommandResult(0, "No job matching 4622a28e\n")
SUPERVISOR_CONFIG = """
[[accounts.account]]
name = "claude-info"
provider = "anthropic"
[accounts.account.env]
CLAUDE_CONFIG_DIR = "~/.claude-accounts/person@example.com"

[[accounts.account]]
name = "codex-chris"
provider = "openai"
[accounts.account.env]
CODEX_HOME = "~/.codex-accounts/person@example.com"

[[accounts.account]]
name = "claude-icloud"
provider = "anthropic"
[accounts.account.env]
CLAUDE_CONFIG_DIR = "/accounts/icloud"

[[accounts.account]]
name = "agy-gemini"
provider = "google"
[accounts.account.env]
GEMINI_HOME = "/accounts/gemini"

[[accounts.account]]
provider = "anthropic"
[accounts.account.env]
CLAUDE_CONFIG_DIR = "/accounts/unnamed"
"""


def make_repositories(root: Path) -> None:
    (root / "alpha" / ".git").mkdir(parents=True)
    (root / "worktree").mkdir()
    (root / "worktree" / ".git").write_text("gitdir: elsewhere\n")
    (root / "plain").mkdir()
    (root / "group" / "nested" / ".git").mkdir(parents=True)


class FakeClaude:
    def __init__(self, *results: CommandResult) -> None:
        self.results = list(results)
        self.calls: list[Command] = []

    async def __call__(self, command: Command) -> CommandResult:
        self.calls.append(command)
        return self.results[min(len(self.calls), len(self.results)) - 1]


def mac_launcher(
    root: Path,
    server_url: str = "ws://127.0.0.1/agent-session/connect",
    run: FakeClaude | None = None,
    supervisor_config: Path | None = None,
) -> MacLauncher:
    config = BridgeConfig(server_url, TOKEN, root / "unused.sock", "Codex on test", reconnect_seconds=0.01)
    launcher = MacLauncher(config, LiveBridges(), supervisor_config, developer_root=root, find_claude=lambda: "/bin/claude")
    if run is not None:
        launcher.run = run
    return launcher


def connected_bridge() -> AsyncMock:
    bridge = AsyncMock(spec=CodexBridge)
    bridge.rpc = object()
    return bridge


class MacLauncherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        make_repositories(self.root)

    def test_repositories_are_direct_children_of_the_root_that_hold_git(self) -> None:
        self.assertEqual(find_repositories(self.root), {"alpha": self.root / "alpha", "worktree": self.root / "worktree"})

    async def test_unknown_repository_names_and_harnesses_launch_nothing(self) -> None:
        claude = FakeClaude(CommandResult(0, CLAUDE_OUTPUT))
        launcher = mac_launcher(self.root, run=claude)
        for repository, harness in [("plain", "claude"), ("../alpha", "claude"), ("nested", "claude"), ("alpha", "bash")]:
            with self.subTest(repository=repository, harness=harness):
                self.assertFalse((await launcher.launch(repository, harness)).ok)
        self.assertEqual(claude.calls, [])

    async def test_codex_launch_starts_a_session_on_the_live_bridge(self) -> None:
        launcher = mac_launcher(self.root)
        bridge = connected_bridge()
        launcher.live.default_socket = Path("/default.sock")
        launcher.live.by_socket[Path("/default.sock")] = cast(CodexBridge, bridge)
        outcome = await launcher.launch("alpha", "codex")
        self.assertTrue(outcome.ok, outcome.message)
        bridge.start_session.assert_awaited_once_with(str(self.root / "alpha"))

    async def test_codex_launch_without_a_connected_bridge_says_so(self) -> None:
        launcher = mac_launcher(self.root)
        self.assertFalse((await launcher.launch("alpha", "codex")).ok)
        bridge = AsyncMock(spec=CodexBridge)
        bridge.rpc = None
        launcher.live.default_socket = Path("/default.sock")
        launcher.live.by_socket[Path("/default.sock")] = cast(CodexBridge, bridge)
        self.assertFalse((await launcher.launch("alpha", "codex")).ok)
        bridge.start_session.assert_not_awaited()

    async def test_claude_launch_runs_a_background_opus_session_with_the_channel_in_the_repository(self) -> None:
        claude = FakeClaude(CommandResult(0, CLAUDE_OUTPUT))
        outcome = await mac_launcher(self.root, run=claude).launch("worktree", "claude")
        self.assertEqual(
            [(call.argv, call.cwd, call.claude_home) for call in claude.calls],
            [(["/bin/claude", "--bg", "--model", "opus", "--channels", "plugin:dui@discord-blue"], self.root / "worktree", None)],
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.message, CLAUDE_OUTPUT.splitlines()[0])

    async def test_claude_failure_reports_its_exit_and_first_line(self) -> None:
        outcome = await mac_launcher(self.root, run=FakeClaude(CommandResult(2, "error: unknown plugin\n"))).launch(
            "alpha", "claude"
        )
        self.assertFalse(outcome.ok)
        self.assertIn("2", outcome.message)
        self.assertIn("error: unknown plugin", outcome.message)

    def supervisor_config(self) -> Path:
        path = self.root / "supervisor.toml"
        path.write_text(SUPERVISOR_CONFIG)
        return path

    def test_named_accounts_come_only_from_the_supervisor_config_and_carry_no_home(self) -> None:
        self.assertEqual(mac_launcher(self.root).hello()["accounts"], [])
        hello = mac_launcher(self.root, supervisor_config=self.supervisor_config()).hello()
        self.assertEqual(
            hello["accounts"],
            [
                {"name": "claude-info", "harness": "claude"},
                {"name": "codex-chris", "harness": "codex"},
                {"name": "claude-icloud", "harness": "claude"},
            ],
        )
        self.assertNotIn("example.com", json.dumps(hello))
        self.assertNotIn("/accounts/", json.dumps(hello))

    async def test_a_claude_account_runs_claude_with_that_config_dir(self) -> None:
        claude = FakeClaude(CommandResult(0, CLAUDE_OUTPUT))
        launcher = mac_launcher(self.root, run=claude, supervisor_config=self.supervisor_config())
        self.assertTrue((await launcher.launch("alpha", "claude", "claude-info")).ok)
        expected = (Path.home() / ".claude-accounts" / "person@example.com").resolve()
        self.assertEqual([call.claude_home for call in claude.calls], [expected])

    def test_claude_environment_names_only_the_chosen_account(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/inherited"}):
            self.assertNotIn("CLAUDE_CONFIG_DIR", claude_environment(None))
            self.assertEqual(claude_environment(Path("/accounts/info"))["CLAUDE_CONFIG_DIR"], "/accounts/info")

    async def test_a_codex_account_starts_on_that_homes_bridge(self) -> None:
        launcher = mac_launcher(self.root, supervisor_config=self.supervisor_config())
        default, chosen = connected_bridge(), connected_bridge()
        account_home = (Path.home() / ".codex-accounts" / "person@example.com").resolve()
        launcher.live.default_socket = Path("/default.sock")
        launcher.live.by_socket[Path("/default.sock")] = cast(CodexBridge, default)
        launcher.live.by_socket[socket_for_home(account_home)] = cast(CodexBridge, chosen)
        self.assertTrue((await launcher.launch("alpha", "codex", "codex-chris")).ok)
        chosen.start_session.assert_awaited_once_with(str(self.root / "alpha"))
        default.start_session.assert_not_awaited()

    async def test_unknown_or_other_harness_accounts_launch_nothing(self) -> None:
        claude = FakeClaude(CommandResult(0, CLAUDE_OUTPUT))
        launcher = mac_launcher(self.root, run=claude, supervisor_config=self.supervisor_config())
        for harness, account in [
            ("claude", "nobody"),
            ("claude", "codex-chris"),
            ("claude", "agy-gemini"),
            ("codex", "claude-info"),
        ]:
            with self.subTest(harness=harness, account=account):
                self.assertFalse((await launcher.launch("alpha", harness, account)).ok)
        self.assertEqual(claude.calls, [])

    async def test_a_claude_first_message_follows_the_channel_after_a_double_dash(self) -> None:
        claude = FakeClaude(CommandResult(0, CLAUDE_OUTPUT))
        await mac_launcher(self.root, run=claude).launch("alpha", "claude", prompt="--help me fix the login bug")
        self.assertEqual(claude.calls[0].argv[-4:], ["--channels", "plugin:dui@discord-blue", "--", "--help me fix the login bug"])

    async def test_a_codex_first_message_starts_the_new_threads_first_turn(self) -> None:
        rpc = FakeRpc()
        async with running_bridge(rpc) as (bridge, _discord):
            launcher = mac_launcher(self.root)
            launcher.live.default_socket = Path("/default.sock")
            launcher.live.by_socket[Path("/default.sock")] = bridge
            outcome = await launcher.launch("alpha", "codex", prompt="Fix the login bug")
        self.assertTrue(outcome.ok, outcome.message)
        [turn] = rpc.called("turn/start")
        assert turn is not None
        self.assertEqual((turn["threadId"], turn["input"]), ("new-thread", [{"type": "text", "text": "Fix the login bug"}]))

    async def test_ending_a_claude_session_tries_the_default_then_each_claude_account(self) -> None:
        claude = FakeClaude(NO_JOB, CommandResult(0, "Stopped 4622a28e\n"))
        launcher = mac_launcher(self.root, run=claude, supervisor_config=self.supervisor_config())
        outcome = await launcher.end_claude(CLAUDE_SESSION_ID)
        self.assertTrue(outcome.ok, outcome.message)
        info = (Path.home() / ".claude-accounts" / "person@example.com").resolve()
        self.assertEqual([call.claude_home for call in claude.calls], [None, info])
        self.assertEqual(claude.calls[0].argv, ["/bin/claude", "stop", "4622a28e"])

    async def test_a_claude_session_no_account_knows_was_started_in_a_terminal(self) -> None:
        claude = FakeClaude(NO_JOB)
        launcher = mac_launcher(self.root, run=claude, supervisor_config=self.supervisor_config())
        self.assertEqual(await launcher.end_claude(CLAUDE_SESSION_ID), LaunchOutcome(False, STARTED_IN_A_TERMINAL))
        self.assertEqual(len(claude.calls), 3)
        self.assertFalse((await launcher.end_claude("--all")).ok)
        self.assertEqual(len(claude.calls), 3)


class RunCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_cancelled_launch_leaves_no_process_running(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            pid_file = Path(folder) / "pid"
            argv = ["/bin/sh", "-c", f'echo $$ > "{pid_file}"; exec sleep 30']
            launch = asyncio.create_task(run_command(Command(argv, Path(folder))))
            self.assertTrue(await until(lambda: pid_file.exists() and pid_file.read_text().strip() != "", timeout=5))
            launch.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await launch
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)


class SessionEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fixture = self.enterContext(prompt_fixture())
        self.session = self.fixture.session
        self.socket: FakeWebSocket = self.fixture.socket
        self.end_claude = AsyncMock(return_value="Stopped 4622a28e")

    async def end(self) -> str:
        user = cast(discord.Member, cast(object, SimpleNamespace(id=123)))
        return await self.fixture.bridge.controls.send_end_session(self.fixture.thread, user, self.end_claude)

    def as_harness(self, harness: str) -> None:
        self.session.hello = replace(self.session.hello, harness=harness)

    async def test_a_claude_thread_is_ended_through_the_mac_launcher_with_its_session_id(self) -> None:
        self.as_harness("claude")
        self.assertEqual(await self.end(), "Stopped 4622a28e")
        self.end_claude.assert_awaited_once_with(self.session.session_id)
        self.assertEqual(self.socket.sent_json, [])

    async def test_a_codex_thread_is_sent_an_end_session_command(self) -> None:
        self.as_harness("codex")
        await self.end()
        self.assertEqual([message["kind"] for message in self.socket.sent_json], ["end_session"])
        self.end_claude.assert_not_awaited()

    async def test_a_non_operator_ends_nothing(self) -> None:
        for harness in ("claude", "codex"):
            self.as_harness(harness)
            with self.subTest(harness=harness), patch.object(self.fixture.bridge, "is_operator", return_value=False):
                self.assertIn("Only agent session operators", await self.end())
        self.end_claude.assert_not_awaited()
        self.assertEqual(self.socket.sent_json, [])


class Launcher:
    def __init__(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        self.websocket = websocket
        self.frames: list[Json] = []
        self.reader = asyncio.create_task(self.read())

    async def read(self) -> None:
        async for frame in self.websocket:
            if frame.type is aiohttp.WSMsgType.TEXT:
                self.frames.append(frame.json())


class SlashStartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        make_repositories(self.root)
        self.fake = FakeDiscord(latency=0.002)
        self.thread = self.fake.add_thread("anywhere")
        self.running: RunningCog = await self.enterAsyncContext(running_cog(self.fake))
        self.http = await self.enterAsyncContext(aiohttp.ClientSession())

    @property
    def launcher_url(self) -> str:
        return self.running.url.replace("/agent-session/connect", "/agent-session/launcher")

    async def connect_launcher(self, repositories: list[str], accounts: list[Json] | None = None) -> Launcher:
        websocket = await self.http.ws_connect(self.launcher_url, headers={"Authorization": f"Bearer {TOKEN}"})
        hello = {"type": "launcher_hello", "host_label": "Mac", "repositories": repositories, "harnesses": ["codex", "claude"]}
        await websocket.send_json({**hello, "accounts": accounts or []})
        server = self.running.cog.bridge.launcher
        self.assertTrue(await until(lambda: server.hello is not None and list(server.hello.repositories) == repositories, timeout=2))
        launcher = Launcher(websocket)
        self.addAsyncCleanup(websocket.close)
        return launcher

    def start(self, by: Json, repository: str, harness: str, **options: str) -> int:
        return app_command(
            self.running.bot, self.thread, by, "session", "start", {"repository": repository, "harness": harness, **options}
        )

    async def answer(self, interaction_id: int) -> str:
        token = f"token-{interaction_id}"
        if not await until(lambda: token in self.fake.followups_by_token, timeout=10):
            raise AssertionError("/session start was never answered")
        return str(self.fake.followups_by_token[token][0]["content"])

    @contextlib.asynccontextmanager
    async def mac(self, claude: FakeClaude, supervisor_config: Path | None = None) -> AsyncIterator[None]:
        launcher = mac_launcher(self.root, self.running.url, run=claude, supervisor_config=supervisor_config)
        task = asyncio.create_task(launcher.run_forever(self.http))
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_an_operator_starts_claude_through_the_mac_launcher(self) -> None:
        claude = FakeClaude(CommandResult(0, CLAUDE_OUTPUT))
        async with self.mac(claude):
            self.assertTrue(await until(lambda: self.running.cog.bridge.launcher.hello is not None, timeout=5))
            self.assertEqual(self.running.cog.bridge.launcher.repository_choices("ALP"), ["alpha"])
            reply = await self.answer(self.start(OPERATOR, "alpha", "claude"))
        self.assertIn(CLAUDE_OUTPUT.splitlines()[0], reply)
        self.assertEqual([call.cwd for call in claude.calls], [self.root / "alpha"])

    async def test_an_operator_starts_claude_on_a_named_account_with_a_first_message(self) -> None:
        supervisor_config = self.root / "supervisor.toml"
        supervisor_config.write_text(SUPERVISOR_CONFIG)
        claude = FakeClaude(CommandResult(0, CLAUDE_OUTPUT))
        async with self.mac(claude, supervisor_config):
            self.assertTrue(await until(lambda: self.running.cog.bridge.launcher.hello is not None, timeout=5))
            reply = await self.answer(self.start(OPERATOR, "alpha", "claude", account="claude-icloud", message="Fix the login bug"))
        self.assertNotIn("type the first message", reply)
        [call] = claude.calls
        self.assertEqual((call.claude_home, call.argv[-1]), (Path("/accounts/icloud"), "Fix the login bug"))

    async def test_only_operators_see_account_names_and_only_for_the_chosen_harness(self) -> None:
        await self.connect_launcher(
            ["alpha"], [{"name": "claude-info", "harness": "claude"}, {"name": "codex-chris", "harness": "codex"}]
        )
        cog = self.running.cog
        for operator, harness, expected in (
            (True, "claude", ["claude-info"]),
            (True, "codex", ["codex-chris"]),
            (False, "claude", []),
        ):
            with self.subTest(operator=operator, harness=harness), patch.object(cog.bridge, "is_operator", return_value=operator):
                interaction = cast(Any, SimpleNamespace(user=None, namespace=SimpleNamespace(harness=harness)))
                self.assertEqual([choice.value for choice in await cog.account_autocomplete(interaction, "")], expected)

    async def test_an_account_the_mac_did_not_list_for_that_harness_is_refused_before_it_is_sent(self) -> None:
        launcher = await self.connect_launcher(["alpha"], [{"name": "codex-chris", "harness": "codex"}])
        for account in ("nobody", "codex-chris"):
            with self.subTest(account=account):
                self.assertIn("no claude account", await self.answer(self.start(OPERATOR, "alpha", "claude", account=account)))
        self.assertEqual(launcher.frames, [])

    async def test_ending_a_claude_session_asks_the_mac_to_stop_it(self) -> None:
        claude = FakeClaude(CommandResult(0, "Stopped 4622a28e\n"))
        async with self.mac(claude):
            self.assertTrue(await until(lambda: self.running.cog.bridge.launcher.hello is not None, timeout=5))
            self.assertEqual(await self.running.cog.bridge.launcher.end(CLAUDE_SESSION_ID), "Stopped 4622a28e")
        self.assertEqual([call.argv for call in claude.calls], [["/bin/claude", "stop", "4622a28e"]])

    async def test_a_bystander_never_reaches_the_launcher(self) -> None:
        launcher = await self.connect_launcher(["alpha"])
        interaction_id = self.start(BYSTANDER, "alpha", "codex")
        self.assertTrue(await until(lambda: interaction_id in self.fake.interaction_responses, timeout=5))
        self.assertNotIn(f"token-{interaction_id}", self.fake.followups_by_token)
        self.assertFalse(await until(lambda: bool(launcher.frames), timeout=0.5))

    async def test_only_operators_see_repository_names(self) -> None:
        await self.connect_launcher(["alpha"])
        cog = self.running.cog
        for operator, expected in ((True, ["alpha"]), (False, [])):
            with self.subTest(operator=operator), patch.object(cog.bridge, "is_operator", return_value=operator):
                choices = await cog.repository_autocomplete(cast(Any, SimpleNamespace(user=None)), "al")
                self.assertEqual([choice.value for choice in choices], expected)

    async def test_without_a_launcher_the_operator_is_told_it_is_not_connected(self) -> None:
        self.assertEqual(await self.answer(self.start(OPERATOR, "alpha", "codex")), NOT_CONNECTED)

    async def test_a_repository_the_mac_did_not_list_is_refused_before_it_is_sent(self) -> None:
        launcher = await self.connect_launcher(["alpha"])
        reply = await self.answer(self.start(OPERATOR, "../alpha", "codex"))
        self.assertIn("no repository", reply)
        self.assertEqual(launcher.frames, [])

    async def test_a_second_launcher_is_turned_away_while_the_first_is_connected(self) -> None:
        first = await self.connect_launcher(["alpha"])
        second = await self.http.ws_connect(self.launcher_url, headers={"Authorization": f"Bearer {TOKEN}"})
        await second.send_json({"type": "launcher_hello", "host_label": "Mac", "repositories": ["beta"], "harnesses": ["codex"]})
        self.assertEqual((await second.receive()).type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(second.close_code, BUSY_CLOSE_CODE)
        self.assertEqual(self.running.cog.bridge.launcher.repository_choices(""), ["alpha"])
        self.assertFalse(first.websocket.closed)

    async def test_a_waiting_mac_launcher_takes_over_when_the_first_leaves(self) -> None:
        first = await self.connect_launcher(["beta"])
        waiting = mac_launcher(self.root, self.running.url)
        waiting.busy_retry_seconds = 0.05
        task = asyncio.create_task(waiting.run_forever(self.http))
        self.addCleanup(task.cancel)
        await asyncio.sleep(0.2)
        self.assertEqual(self.running.cog.bridge.launcher.repository_choices(""), ["beta"])
        await first.websocket.close()
        self.assertTrue(await until(lambda: "alpha" in self.running.cog.bridge.launcher.repository_choices(""), timeout=5))


if __name__ == "__main__":
    unittest.main()
