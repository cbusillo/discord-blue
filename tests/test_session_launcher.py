from __future__ import annotations

import asyncio
import contextlib
import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from discord_blue.codex_bridge.bridge import CodexBridge
from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.codex_bridge.launcher import CommandResult, LiveBridge, MacLauncher, find_repositories
from discord_blue.doodads.agent_session.launcher import NOT_CONNECTED
from tests.discord_client import BYSTANDER, OPERATOR, TOKEN, RunningCog, app_command, running_cog
from tests.fake_discord import FakeDiscord
from tests.test_attach_scenarios import until

Json = dict[str, Any]
CLAUDE_OUTPUT = "backgrounded · 4622a28e · alpha (idle — send a prompt to start)\nmore detail\n"


def make_repositories(root: Path) -> None:
    (root / "alpha" / ".git").mkdir(parents=True)
    (root / "worktree").mkdir()
    (root / "worktree" / ".git").write_text("gitdir: elsewhere\n")
    (root / "plain").mkdir()
    (root / "group" / "nested" / ".git").mkdir(parents=True)


class FakeClaude:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[tuple[list[str], Path]] = []

    async def __call__(self, argv: list[str], cwd: Path) -> CommandResult:
        self.calls.append((argv, cwd))
        return self.result


def mac_launcher(root: Path, server_url: str = "ws://127.0.0.1/agent-session/connect", run: FakeClaude | None = None) -> MacLauncher:
    config = BridgeConfig(server_url, TOKEN, root / "unused.sock", "Codex on test", reconnect_seconds=0.01)
    launcher = MacLauncher(config, LiveBridge(), developer_root=root, find_claude=lambda: "/bin/claude")
    if run is not None:
        launcher.run = run
    return launcher


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
        bridge = AsyncMock(spec=CodexBridge)
        bridge.rpc = object()
        launcher.live.bridge = cast(CodexBridge, bridge)
        outcome = await launcher.launch("alpha", "codex")
        self.assertTrue(outcome.ok, outcome.message)
        bridge.start_session.assert_awaited_once_with(str(self.root / "alpha"))

    async def test_codex_launch_without_a_connected_bridge_says_so(self) -> None:
        launcher = mac_launcher(self.root)
        self.assertFalse((await launcher.launch("alpha", "codex")).ok)
        bridge = AsyncMock(spec=CodexBridge)
        bridge.rpc = None
        launcher.live.bridge = cast(CodexBridge, bridge)
        self.assertFalse((await launcher.launch("alpha", "codex")).ok)
        bridge.start_session.assert_not_awaited()

    async def test_claude_launch_runs_a_background_opus_session_with_the_channel_in_the_repository(self) -> None:
        claude = FakeClaude(CommandResult(0, CLAUDE_OUTPUT))
        outcome = await mac_launcher(self.root, run=claude).launch("worktree", "claude")
        self.assertEqual(
            claude.calls,
            [
                (
                    ["/bin/claude", "--bg", "--model", "opus", "--channels", "plugin:dui@discord-blue"],
                    self.root / "worktree",
                )
            ],
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

    async def connect_launcher(self, repositories: list[str]) -> Launcher:
        websocket = await self.http.ws_connect(self.launcher_url, headers={"Authorization": f"Bearer {TOKEN}"})
        await websocket.send_json(
            {"type": "launcher_hello", "host_label": "Mac", "repositories": repositories, "harnesses": ["codex", "claude"]}
        )
        server = self.running.cog.bridge.launcher
        self.assertTrue(await until(lambda: server.hello is not None and list(server.hello.repositories) == repositories, timeout=2))
        launcher = Launcher(websocket)
        self.addAsyncCleanup(websocket.close)
        return launcher

    def start(self, by: Json, repository: str, harness: str) -> int:
        return app_command(self.running.bot, self.thread, by, "session", "start", {"repository": repository, "harness": harness})

    async def answer(self, interaction_id: int) -> str:
        token = f"token-{interaction_id}"
        if not await until(lambda: token in self.fake.followups_by_token, timeout=10):
            raise AssertionError("/session start was never answered")
        return str(self.fake.followups_by_token[token][0]["content"])

    @contextlib.asynccontextmanager
    async def mac(self, claude: FakeClaude) -> AsyncIterator[None]:
        launcher = mac_launcher(self.root, self.running.url, run=claude)
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
        self.assertEqual([cwd for _, cwd in claude.calls], [self.root / "alpha"])

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

    async def test_a_newer_launcher_replaces_the_older(self) -> None:
        older = await self.connect_launcher(["alpha"])
        newer = await self.connect_launcher(["beta"])
        await asyncio.wait_for(older.reader, 2)
        interaction_id = self.start(OPERATOR, "beta", "codex")
        self.assertTrue(await until(lambda: bool(newer.frames), timeout=5))
        launch = newer.frames[0]
        self.assertEqual((launch["type"], launch["repository"], launch["harness"]), ("launch", "beta", "codex"))
        await newer.websocket.send_json({"type": "launch_result", "launch_id": launch["launch_id"], "ok": False, "message": "nope"})
        self.assertEqual(await self.answer(interaction_id), "Launch failed: nope")


if __name__ == "__main__":
    unittest.main()
