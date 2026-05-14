from __future__ import annotations

import asyncio
import json
import os
import importlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from tests.fakes_every_code import FakeBot
from tests.fakes_every_code import FetchOnlyFakeBot
from tests.fakes_every_code import FakeInteraction
from tests.fakes_every_code import FakeReplyMessage
from tests.fakes_every_code import FakeTextChannel
from tests.fakes_every_code import FakeThread
from tests.fakes_every_code import FakeWebSocket
from tests.fakes_every_code import add_bot_message
from tests.fakes_every_code import make_hello

_TEST_HOME = tempfile.TemporaryDirectory()
os.environ["HOME"] = _TEST_HOME.name
_CONFIG_PATH = Path(_TEST_HOME.name) / ".config" / "discord-blue" / "config.toml"
_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)


def write_default_config() -> None:
    _CONFIG_PATH.write_text(
        "\n".join(
            [
                "[discord]",
                'token = "from-test"',
                "guild_id = 1",
                "bot_channel_id = 2",
                'employee_role_name = "employee"',
                "loaded_doodads = []",
                "",
                "[every_code]",
                "enabled = false",
                'listen_host = "0.0.0.0"',
                "listen_port = 8787",
                'token = ""',
                "channel_id = 0",
                'operator_role_name = ""',
                "auto_join_user_ids = []",
                "heartbeat_timeout_seconds = 120",
                "heartbeat_check_interval_seconds = 30",
            ]
        )
    )


write_default_config()

Config = importlib.import_module("discord_blue.config").Config
bridge_module = importlib.import_module("discord_blue.doodads.every_code.bridge")
EveryCodeBridge = bridge_module.EveryCodeBridge
messages_module = importlib.import_module("discord_blue.doodads.every_code.messages")
protocol_module = importlib.import_module("discord_blue.doodads.every_code.protocol")
RemoteCommand = protocol_module.RemoteCommand
RemoteApprovalRequest = protocol_module.RemoteApprovalRequest
RemoteRequestUserInput = protocol_module.RemoteRequestUserInput
RequestUserInputQuestion = protocol_module.RequestUserInputQuestion
RequestUserInputQuestionOption = protocol_module.RequestUserInputQuestionOption
SessionHello = protocol_module.SessionHello
SessionMetadataChanged = protocol_module.SessionMetadataChanged
SessionOrigin = protocol_module.SessionOrigin
SessionStatus = protocol_module.SessionStatus
sessions_module = importlib.import_module("discord_blue.doodads.every_code.sessions")
EveryCodeSession = sessions_module.EveryCodeSession
EveryCodeSessionRegistry = sessions_module.EveryCodeSessionRegistry
PendingRemoteApproval = sessions_module.PendingRemoteApproval
threads_module = importlib.import_module("discord_blue.doodads.every_code.threads")
create_session_thread = threads_module.create_session_thread
session_notification_message = threads_module.session_notification_message
session_start_message = threads_module.session_start_message
session_thread_name = threads_module.session_thread_name


def stub_recovered_assistant_message(bridge: object, recovered_message: str | None) -> None:
    cast(Any, bridge).recover_latest_assistant_message = lambda _hello: recovered_message


class ConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        write_default_config()

    def test_config_module_import_has_no_filesystem_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            env = os.environ.copy()
            env["HOME"] = home
            result = subprocess.run(
                [sys.executable, "-c", "import discord_blue.config"],
                cwd=Path(__file__).parents[1],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((Path(home) / ".config" / "discord-blue" / "config.toml").exists())

    def test_every_code_config_loads_and_saves(self) -> None:
        _CONFIG_PATH.write_text(
            "\n".join(
                [
                    "[discord]",
                    'token = "from-file"',
                    "guild_id = 1",
                    "bot_channel_id = 2",
                    'employee_role_name = "employee"',
                    'loaded_doodads = ["every_code_doodad"]',
                    "",
                    "[every_code]",
                    "enabled = true",
                    'listen_host = "127.0.0.1"',
                    "listen_port = 8788",
                    'token = "shared-secret"',
                    "channel_id = 3",
                    'operator_role_name = "code-operator"',
                    "auto_join_user_ids = [10, 11]",
                    "heartbeat_timeout_seconds = 45",
                    "heartbeat_check_interval_seconds = 5",
                ]
            )
        )

        config = Config()

        self.assertTrue(config.every_code.enabled)
        self.assertEqual(config.every_code.listen_host, "127.0.0.1")
        self.assertEqual(config.every_code.listen_port, 8788)
        self.assertEqual(config.every_code.token, "shared-secret")
        self.assertEqual(config.every_code.channel_id, 3)
        self.assertEqual(config.every_code.operator_role_name, "code-operator")
        self.assertEqual(config.every_code.auto_join_user_ids, [10, 11])
        self.assertEqual(config.every_code.heartbeat_timeout_seconds, 45)
        self.assertEqual(config.every_code.heartbeat_check_interval_seconds, 5)
        saved = _CONFIG_PATH.read_text()
        self.assertIn("[every_code]", saved)
        self.assertIn('token = "shared-secret"', saved)


class SessionRegistryTests(unittest.TestCase):
    def test_session_registration_binds_thread_mapping(self) -> None:
        registry = EveryCodeSessionRegistry()
        session = EveryCodeSession(hello=make_hello(), websocket=FakeWebSocket())

        registry.register(session)
        registry.bind_thread("session-1", 555, notification_message_id=777)

        self.assertIs(registry.get("session-1"), session)
        self.assertIs(registry.get_by_thread(555), session)
        self.assertEqual(session.thread_id, 555)
        self.assertEqual(session.notification_message_id, 777)
        self.assertIs(registry.remove("session-1"), session)
        self.assertIsNone(registry.get_by_thread(555))


class ProtocolTests(unittest.TestCase):
    def test_session_hello_from_payload_applies_defaults(self) -> None:
        hello = SessionHello.from_payload(
            {
                "session_id": "session-1",
                "session_epoch": "epoch-1",
                "cwd": "/tmp/project",
            }
        )

        self.assertEqual(hello.session_id, "session-1")
        self.assertEqual(hello.session_epoch, "epoch-1")
        self.assertEqual(hello.host_label, "Every Code")
        self.assertEqual(hello.cwd, "/tmp/project")
        self.assertIsNone(hello.branch)
        self.assertEqual(hello.pid, 0)
        self.assertIsNone(hello.origin)

    def test_session_hello_from_payload_parses_origin(self) -> None:
        hello = SessionHello.from_payload(
            {
                "session_id": "session-1",
                "session_epoch": "epoch-1",
                "cwd": "/tmp/project",
                "origin": {
                    "kind": "every_code",
                    "request_id": "every-code-cbusillo-syo-67",
                    "repository": "cbusillo/sellyouroutboard",
                    "issue_number": 67,
                    "issue_url": "https://github.com/cbusillo/sellyouroutboard/issues/67",
                },
            }
        )

        self.assertIsNotNone(hello.origin)
        assert hello.origin is not None
        self.assertEqual(hello.origin.kind, "every_code")
        self.assertEqual(hello.origin.repository, "cbusillo/sellyouroutboard")
        self.assertEqual(hello.origin.issue_number, 67)

    def test_session_metadata_changed_from_payload(self) -> None:
        metadata = SessionMetadataChanged.from_payload(
            {
                "session_id": "session-1",
                "session_epoch": "epoch-1",
                "cwd": "/tmp/project-worktree",
                "branch": "code/project-task",
                "reason": "working_branch_selected",
            }
        )

        self.assertEqual(metadata.session_id, "session-1")
        self.assertEqual(metadata.session_epoch, "epoch-1")
        self.assertEqual(metadata.cwd, "/tmp/project-worktree")
        self.assertEqual(metadata.branch, "code/project-task")
        self.assertEqual(metadata.reason, "working_branch_selected")

    def test_remote_command_serializes_bridge_message(self) -> None:
        command = RemoteCommand(
            command_id="cmd-1",
            session_id="session-1",
            session_epoch="epoch-1",
            kind="reply",
            text="run tests",
            issued_by="123",
        )

        self.assertEqual(
            command.to_message(),
            {
                "type": "command",
                "command_id": "cmd-1",
                "session_id": "session-1",
                "session_epoch": "epoch-1",
                "kind": "reply",
                "text": "run tests",
                "issued_by": "123",
            },
        )

    def test_continue_command_serializes_bridge_message(self) -> None:
        command = RemoteCommand(
            command_id="cmd-1",
            session_id="session-1",
            session_epoch="epoch-1",
            kind="continue_autonomously",
            issued_by="123",
        )

        self.assertEqual(
            command.to_message(),
            {
                "type": "command",
                "command_id": "cmd-1",
                "session_id": "session-1",
                "session_epoch": "epoch-1",
                "kind": "continue_autonomously",
                "text": None,
                "issued_by": "123",
            },
        )

    def test_request_user_input_from_payload_preserves_questions(self) -> None:
        request = RemoteRequestUserInput.from_payload(
            {
                "call_id": "call-1",
                "turn_id": "turn-1",
                "session_id": "session-1",
                "session_epoch": "epoch-1",
                "questions": [
                    {
                        "id": "mode",
                        "header": "Build mode",
                        "question": "Choose a mode",
                        "options": [
                            {"label": "Fast", "description": "Skip extras"},
                            {"label": "Safe", "description": "Full validation"},
                        ],
                    }
                ],
            }
        )

        self.assertEqual(request.call_id, "call-1")
        self.assertEqual(request.turn_id, "turn-1")
        self.assertEqual(request.session_id, "session-1")
        self.assertEqual(request.questions[0].header, "Build mode")
        self.assertEqual(request.questions[0].options[1].label, "Safe")

    def test_request_user_input_response_command_serializes_bridge_message(self) -> None:
        command = RemoteCommand(
            command_id="cmd-1",
            session_id="session-1",
            session_epoch="epoch-1",
            kind="request_user_input_response",
            call_id="call-1",
            turn_id="turn-1",
            response={"answers": {"mode": {"answers": ["Safe"]}}},
            issued_by="123",
        )

        self.assertEqual(
            command.to_message(),
            {
                "type": "command",
                "command_id": "cmd-1",
                "session_id": "session-1",
                "session_epoch": "epoch-1",
                "kind": "request_user_input_response",
                "text": None,
                "call_id": "call-1",
                "turn_id": "turn-1",
                "response": {"answers": {"mode": {"answers": ["Safe"]}}},
                "issued_by": "123",
            },
        )


class ThreadFormattingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_text_channel_type = threads_module.discord.TextChannel
        threads_module.discord.TextChannel = FakeTextChannel
        messages_module.MISSING_MANAGE_MESSAGES_DESTINATIONS.clear()

    async def asyncTearDown(self) -> None:
        threads_module.discord.TextChannel = self.original_text_channel_type
        messages_module.MISSING_MANAGE_MESSAGES_DESTINATIONS.clear()

    async def test_create_session_thread_suppresses_embeds_on_start_messages(self) -> None:
        config = Config()
        config.every_code.channel_id = 321
        channel = FakeTextChannel(321, [])

        session_thread = await create_session_thread(FakeBot(config, channel=channel), make_hello())

        self.assertTrue(channel.sent_kwargs[0]["suppress_embeds"])
        self.assertTrue(session_thread.thread.sent_kwargs[0]["suppress_embeds"])

    async def test_create_session_thread_warns_when_manage_messages_missing(self) -> None:
        config = Config()
        config.every_code.channel_id = 321
        channel = FakeTextChannel(321, [], manage_messages=False)

        session_thread = await create_session_thread(FakeBot(config, channel=channel), make_hello())

        self.assertNotIn("suppress_embeds", channel.sent_kwargs[0])
        self.assertIn("missing the `Manage Messages` permission", channel.sent_messages[1])
        self.assertNotIn("suppress_embeds", session_thread.thread.sent_kwargs[0])
        self.assertIn("missing the `Manage Messages` permission", session_thread.thread.sent_messages[1])

    async def test_missing_manage_messages_notice_posts_once_per_destination(self) -> None:
        config = Config()
        config.every_code.channel_id = 321
        channel = FakeTextChannel(321, [], manage_messages=False)

        await create_session_thread(FakeBot(config, channel=channel), make_hello())
        await messages_module.send_every_code_message(channel, "Second message")

        notices = [message for message in channel.sent_messages if "missing the `Manage Messages` permission" in message]
        self.assertEqual(len(notices), 1)

    async def test_missing_manage_messages_notice_retries_after_send_failure(self) -> None:
        thread = FakeThread(555, manage_messages=False)
        thread.send_failures_remaining = 1

        await messages_module.notify_missing_manage_messages(thread)
        await messages_module.send_every_code_message(thread, "Second message")

        notices = [message for message in thread.sent_messages if "missing the `Manage Messages` permission" in message]
        self.assertEqual(len(notices), 1)

    async def test_missing_manage_messages_notice_is_atomic_per_destination(self) -> None:
        thread = FakeThread(555, manage_messages=False)

        await asyncio.gather(
            messages_module.send_every_code_message(thread, "First message"),
            messages_module.send_every_code_message(thread, "Second message"),
        )

        notices = [message for message in thread.sent_messages if "missing the `Manage Messages` permission" in message]
        self.assertEqual(len(notices), 1)

    def test_session_thread_text_uses_repo_branch_and_no_mentions(self) -> None:
        hello = make_hello()
        thread = SimpleNamespace(id=555)

        self.assertEqual(session_thread_name(hello), "project · sess")
        self.assertEqual(
            session_notification_message(hello, thread),
            "Every Code session connected for `project · sess`: <#555>",
        )
        self.assertEqual(
            session_start_message(hello),
            "\n".join(
                [
                    "Every Code session connected",
                    "",
                    "host: Mac Studio",
                    "cwd: `/tmp/project`",
                    "branch: `main`",
                    "pid: `42`",
                ]
            ),
        )

    def test_session_thread_text_handles_missing_branch_and_empty_cwd(self) -> None:
        hello = SessionHello(
            session_id="session-1",
            session_epoch="epoch-1",
            host_label="Every Code",
            cwd="",
            branch=None,
            pid=0,
        )
        thread = SimpleNamespace(id=555)

        self.assertEqual(session_thread_name(hello), "session · sess")
        self.assertEqual(
            session_notification_message(hello, thread),
            "Every Code session connected for `session · sess`: <#555>",
        )
        self.assertIn("branch: `unknown`", session_start_message(hello))

    def test_session_thread_text_includes_non_default_branch(self) -> None:
        hello = make_hello()
        hello.branch = "code/project-task"
        thread = SimpleNamespace(id=555)

        self.assertEqual(session_thread_name(hello), "project · code/project-task · sess")
        self.assertEqual(
            session_notification_message(hello, thread),
            "Every Code session connected for `project · code/project-task · sess`: <#555>",
        )

    def test_session_thread_text_uses_start_time_and_short_session_id(self) -> None:
        hello = make_hello()
        hello.session_id = "05ddf473-6cf3-4850-ac00-8f0a158d0c9d"
        hello.session_epoch = "1710000000000"

        self.assertRegex(session_thread_name(hello), r"^project · \d{2}:\d{2} 05dd$")

    def test_session_thread_text_omits_common_default_branch_names(self) -> None:
        thread = SimpleNamespace(id=555)

        for branch in ["main", "master", "develop", "development", "dev", "trunk", "MAIN"]:
            hello = make_hello()
            hello.branch = branch

            self.assertEqual(session_thread_name(hello), "project · sess")
            self.assertEqual(
                session_notification_message(hello, thread),
                "Every Code session connected for `project · sess`: <#555>",
            )

    def test_session_thread_text_marks_every_code_origin(self) -> None:
        hello = SessionHello(
            session_id="session-1",
            session_epoch="epoch-1",
            host_label="Mac Studio",
            cwd="/tmp/worktree",
            branch="every-code/cbusillo-syo-67",
            pid=42,
            origin=SessionOrigin(
                kind="every_code",
                request_id="every-code-cbusillo-syo-67",
                repository="cbusillo/sellyouroutboard",
                issue_number=67,
                issue_url="https://github.com/cbusillo/sellyouroutboard/issues/67",
            ),
        )
        thread = SimpleNamespace(id=555)

        self.assertEqual(
            session_thread_name(hello),
            "EC sellyouroutboard#67",
        )
        self.assertEqual(
            session_notification_message(hello, thread),
            "Every Code automated session connected for `cbusillo/sellyouroutboard#67` on `every-code/cbusillo-syo-67`: <#555>",
        )
        start_message = session_start_message(hello)
        self.assertIn("origin: `Every Code automation`", start_message)
        self.assertIn("source: `cbusillo/sellyouroutboard#67`", start_message)
        self.assertIn("issue: https://github.com/cbusillo/sellyouroutboard/issues/67", start_message)
        self.assertIn("request: `every-code-cbusillo-syo-67`", start_message)

    def test_session_thread_name_caps_human_branch_length(self) -> None:
        hello = SessionHello(
            session_id="session-1",
            session_epoch="epoch-1",
            host_label="Mac Studio",
            cwd="/tmp/project",
            branch="feature/" + "x" * 160,
            pid=42,
        )

        thread_name = session_thread_name(hello)

        self.assertLessEqual(len(thread_name), 100)
        self.assertTrue(thread_name.endswith("…"))


class FakeThreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_creates_bot_authored_history_message(self) -> None:
        thread = FakeThread(555)

        message = await thread.send("Every Code session connected")

        self.assertEqual(message.author.id, 999)
        self.assertEqual((await thread.fetch_message(message.id)).author.id, 999)
        history = [entry async for entry in thread.history(limit=10, oldest_first=True)]
        self.assertEqual([entry.id for entry in history], [message.id])

    async def test_delete_removes_message_from_fetch_and_history(self) -> None:
        thread = FakeThread(555)

        message = await thread.send("Every Code session connected")
        await message.delete()

        self.assertTrue(message.deleted)
        with self.assertRaises(bridge_module.discord.NotFound):
            await thread.fetch_message(message.id)
        history = [entry async for entry in thread.history(limit=10, oldest_first=True)]
        self.assertEqual(history, [])


# noinspection DuplicatedCode
class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        write_default_config()
        self.original_thread_type = bridge_module.discord.Thread
        self.original_text_channel_type = bridge_module.discord.TextChannel
        bridge_module.discord.Thread = FakeThread
        bridge_module.discord.TextChannel = FakeTextChannel

    async def asyncTearDown(self) -> None:
        bridge_module.discord.Thread = self.original_thread_type
        bridge_module.discord.TextChannel = self.original_text_channel_type

    async def test_websocket_auth_rejects_missing_or_wrong_token(self) -> None:
        config = Config()
        config.every_code.token = "shared-secret"
        bridge = EveryCodeBridge(FakeBot(config))

        self.assertFalse(bridge._authorized(SimpleNamespace(headers={})))
        self.assertFalse(bridge._authorized(SimpleNamespace(headers={"Authorization": "Bearer wrong"})))
        self.assertTrue(bridge._authorized(SimpleNamespace(headers={"Authorization": "Bearer shared-secret"})))

    async def test_cleanup_stale_session_notifications_deletes_human_and_automated_notices(self) -> None:
        config = Config()
        config.every_code.channel_id = 321
        channel = FakeTextChannel(321, [])
        human_notice = add_bot_message(
            channel,
            101,
            "Every Code session connected for `project` on `main`: <#555>",
        )
        automated_notice = add_bot_message(
            channel,
            102,
            "Every Code automated session connected for `cbusillo/sellyouroutboard#67`: <#556>",
        )
        unrelated_bot_notice = add_bot_message(channel, 103, "Every Code status summary")
        user_notice = FakeReplyMessage(
            104,
            channel,
            "Every Code automated session connected for `user/post`: <#557>",
            author_id=123,
        )
        channel.add_message(user_notice)
        bridge = EveryCodeBridge(FakeBot(config, channel=channel))

        await bridge.cleanup_stale_session_notifications()

        self.assertTrue(human_notice.deleted)
        self.assertTrue(automated_notice.deleted)
        self.assertFalse(unrelated_bot_notice.deleted)
        self.assertFalse(user_notice.deleted)

    async def test_stop_disconnects_sessions_before_runner_cleanup(self) -> None:
        config = Config()
        bridge = EveryCodeBridge(FakeBot(config))
        websocket = FakeWebSocket()
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=websocket,
            thread_id=555,
            notification_message_id=777,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555, notification_message_id=777)
        test_case = self

        class FakeRunner:
            def __init__(self) -> None:
                self.cleaned = False

            async def cleanup(self) -> None:
                self.cleaned = True
                test_case.assertTrue(websocket.closed)
                test_case.assertIsNone(bridge.sessions.get("session-1"))

        runner = FakeRunner()
        cast(Any, bridge)._runner = runner

        await bridge.stop()

        self.assertTrue(runner.cleaned)
        self.assertTrue(websocket.closed)
        self.assertEqual(websocket.close_messages, [b"bridge shutdown"])
        self.assertIsNone(bridge.sessions.get("session-1"))
        self.assertIsNone(bridge.sessions.get_by_thread(555))
        self.assertIsNone(cast(Any, bridge)._runner)

    async def test_stop_disconnects_sessions_concurrently(self) -> None:
        config = Config()
        bridge = EveryCodeBridge(FakeBot(config))
        all_closes_started = asyncio.Event()
        started_closes = 0
        session_count = 3

        class CoordinatedWebSocket(FakeWebSocket):
            async def close(self, *, message: bytes = b"", drain: bool = True) -> bool:
                nonlocal started_closes
                started_closes += 1
                if started_closes == session_count:
                    all_closes_started.set()
                await all_closes_started.wait()
                return await super().close(message=message, drain=drain)

        websockets: list[CoordinatedWebSocket] = []
        for index in range(session_count):
            websocket = CoordinatedWebSocket()
            websockets.append(websocket)
            session_id = f"session-{index}"
            session = EveryCodeSession(
                hello=SessionHello(
                    session_id=session_id,
                    session_epoch="epoch-1",
                    host_label="Mac Studio",
                    cwd="/tmp/project",
                    branch="main",
                    pid=42,
                ),
                websocket=websocket,
                thread_id=555 + index,
            )
            bridge.sessions.register(session)
            bridge.sessions.bind_thread(session_id, 555 + index)

        class FakeRunner:
            async def cleanup(self) -> None:
                pass

        bridge_module_any = cast(Any, bridge_module)
        original_timeout = bridge_module_any.SHUTDOWN_WEBSOCKET_CLOSE_TIMEOUT_SECONDS
        bridge_module_any.SHUTDOWN_WEBSOCKET_CLOSE_TIMEOUT_SECONDS = 0.25
        cast(Any, bridge)._runner = FakeRunner()
        try:
            await bridge.stop()
        finally:
            bridge_module_any.SHUTDOWN_WEBSOCKET_CLOSE_TIMEOUT_SECONDS = original_timeout

        self.assertEqual(started_closes, session_count)
        for websocket in websockets:
            self.assertEqual(websocket.close_messages, [b"bridge shutdown"])
            self.assertTrue(websocket.closed)

    async def test_stop_bounds_slow_runner_cleanup(self) -> None:
        config = Config()
        bridge = EveryCodeBridge(FakeBot(config))

        class SlowRunner:
            def __init__(self) -> None:
                self.cleanup_started = False
                self.cleanup_cancelled = False

            async def cleanup(self) -> None:
                self.cleanup_started = True
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    self.cleanup_cancelled = True
                    raise

        runner = SlowRunner()
        bridge_module_any = cast(Any, bridge_module)
        original_timeout = bridge_module_any.SHUTDOWN_RUNNER_CLEANUP_TIMEOUT_SECONDS
        bridge_module_any.SHUTDOWN_RUNNER_CLEANUP_TIMEOUT_SECONDS = 0.01
        cast(Any, bridge)._runner = runner
        try:
            with self.assertLogs(cast(Any, bridge_module).logger, level="WARNING") as logs:
                await bridge.stop()
        finally:
            bridge_module_any.SHUTDOWN_RUNNER_CLEANUP_TIMEOUT_SECONDS = original_timeout

        self.assertIn("Every Code bridge runner cleanup timed out during shutdown", "\n".join(logs.output))
        self.assertTrue(runner.cleanup_started)
        self.assertTrue(runner.cleanup_cancelled)
        self.assertIsNone(cast(Any, bridge)._runner)

    async def test_thread_reply_routes_to_registered_session_websocket(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=websocket,
            thread_id=555,
            control_message_id=901,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        message = FakeReplyMessage(777, thread, "run the focused test")
        thread.add_message(message)

        handled = await bridge.send_thread_reply(message)

        self.assertTrue(handled)
        self.assertEqual(len(websocket.sent_json), 1)
        sent = websocket.sent_json[0]
        self.assertEqual(sent["type"], "command")
        self.assertEqual(sent["session_id"], "session-1")
        self.assertEqual(sent["session_epoch"], "epoch-1")
        self.assertEqual(sent["kind"], "reply")
        self.assertEqual(sent["text"], "run the focused test")
        self.assertEqual(sent["issued_by"], "123")
        self.assertIn(str(sent["command_id"]), session.pending_commands)
        self.assertEqual(message.reactions, [bridge_module.REACTION_QUEUED])

    async def test_thread_reply_reports_offline_session(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket(closed=True)
        session = EveryCodeSession(hello=make_hello(), websocket=websocket, thread_id=555)
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        message = FakeReplyMessage(777, thread, "hello?")
        thread.add_message(message)

        handled = await bridge.send_thread_reply(message)

        self.assertTrue(handled)
        self.assertEqual(websocket.sent_json, [])
        self.assertEqual(
            message.replies,
            ["Every Code session is offline; reply was not delivered."],
        )

    async def test_continue_autonomously_routes_to_registered_session_websocket(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        session = EveryCodeSession(hello=make_hello(), websocket=websocket, thread_id=555)
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        response = await bridge.send_continue_autonomously(thread, SimpleNamespace(id=123))

        self.assertEqual(response, "Asked Every Code to go ahead until it needs you.")
        self.assertEqual(len(websocket.sent_json), 1)
        sent = websocket.sent_json[0]
        self.assertEqual(sent["type"], "command")
        self.assertEqual(sent["session_id"], "session-1")
        self.assertEqual(sent["session_epoch"], "epoch-1")
        self.assertEqual(sent["kind"], "continue_autonomously")
        self.assertIsNone(sent["text"])
        self.assertEqual(sent["issued_by"], "123")
        pending = session.pending_commands[str(sent["command_id"])]
        self.assertEqual(pending.thread_id, 555)
        self.assertIsNone(pending.message_id)
        self.assertEqual(pending.reject_notice, "Every Code could not go ahead")

    async def test_continue_autonomously_reports_reject_in_thread(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        session = EveryCodeSession(hello=make_hello(), websocket=websocket, thread_id=555)
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.send_continue_autonomously(thread, SimpleNamespace(id=123))
        command_id = str(websocket.sent_json[0]["command_id"])
        await bridge.handle_command_reject(
            {
                "command_id": command_id,
                "session_id": "session-1",
                "session_epoch": "epoch-1",
                "reason": "Auto Drive is already running",
            }
        )

        self.assertEqual(
            thread.sent_messages,
            ["Every Code could not go ahead: Auto Drive is already running"],
        )
        self.assertNotIn(command_id, session.pending_commands)

    async def test_new_session_routes_to_registered_session_websocket(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        session = EveryCodeSession(hello=make_hello(), websocket=websocket, thread_id=555)
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        response = await bridge.send_new_session(thread, SimpleNamespace(id=123))

        self.assertEqual(response, "Asked Every Code to start a new session in this folder.")
        self.assertEqual(len(websocket.sent_json), 1)
        sent = websocket.sent_json[0]
        self.assertEqual(sent["type"], "command")
        self.assertEqual(sent["session_id"], "session-1")
        self.assertEqual(sent["session_epoch"], "epoch-1")
        self.assertEqual(sent["kind"], "new_session")
        self.assertIsNone(sent["text"])
        self.assertEqual(sent["issued_by"], "123")
        pending = session.pending_commands[str(sent["command_id"])]
        self.assertEqual(pending.thread_id, 555)
        self.assertIsNone(pending.message_id)
        self.assertEqual(pending.kind, "new_session")
        self.assertEqual(pending.reject_notice, "Every Code could not start a new session")

    async def test_go_ahead_interaction_replies_ephemerally(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        session = EveryCodeSession(hello=make_hello(), websocket=websocket, thread_id=555)
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        interaction = FakeInteraction(thread)

        await bridge.handle_go_ahead_interaction(interaction)

        self.assertEqual(
            interaction.response.messages,
            [("Asked Every Code to go ahead until it needs you.", True)],
        )
        self.assertEqual(websocket.sent_json[0]["kind"], "continue_autonomously")

    async def test_go_ahead_interaction_preserves_the_control_anchor(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=websocket,
            thread_id=555,
            control_message_id=901,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        control_message = FakeReplyMessage(901, thread, "Every Code `project` on `main`")
        thread.add_message(control_message)
        interaction = FakeInteraction(thread, message=control_message)

        await bridge.handle_go_ahead_interaction(interaction)

        self.assertFalse(control_message.deleted)
        self.assertEqual(session.control_message_id, 901)
        self.assertEqual(control_message.reactions, [bridge_module.REACTION_QUEUED])
        self.assertEqual(websocket.sent_json[0]["kind"], "continue_autonomously")

    async def test_tui_user_message_rebinds_pending_control_command_to_new_anchor(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
            control_message_id=910,
        )
        session.pending_commands["cmd-1"] = sessions_module.PendingRemoteCommand(
            thread_id=555,
            message_id=910,
            kind="continue_autonomously",
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        control_message = FakeReplyMessage(910, thread, "\u200b")
        thread.add_message(control_message)

        await bridge.handle_user_message(
            protocol_module.UserMessage(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Run the quick path",
            )
        )

        self.assertTrue(control_message.deleted)
        self.assertEqual(
            thread.sent_messages,
            [
                "**You**\n>>> Run the quick path",
                "\u200b",
            ],
        )
        self.assertEqual(session.control_message_id, 902)
        self.assertEqual(session.pending_commands["cmd-1"].message_id, 902)
        self.assertTrue(session.control_interruptions_enabled)

    async def test_turn_complete_posts_contextual_controls(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_session_status(
            "turn_complete",
            SessionStatus(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Waiting for direction",
                assistant_message="Done.",
            ),
        )

        self.assertEqual(
            thread.sent_messages,
            [
                "**Assistant**\nDone.",
                "\u200b",
            ],
        )
        self.assertIsNone(thread.sent_views[0])
        self.assertIsNone(thread.sent_views[1])
        control_message = await thread.fetch_message(902)
        self.assertEqual(control_message.reactions, ["▶️", bridge_module.REACTION_CONTROL_STATUS, "⏹️"])
        self.assertEqual(session.control_message_id, 902)

    async def test_turn_complete_keeps_split_code_fences_balanced(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        assistant_message = "```python\n" + "print('hello')\n" * 180 + "```"

        await bridge.handle_session_status(
            "turn_complete",
            SessionStatus(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Waiting for direction",
                assistant_message=assistant_message,
            ),
        )

        assistant_messages = [message for message in thread.sent_messages if message.startswith("**Assistant**\n")]
        self.assertGreater(len(assistant_messages), 1)
        for message in assistant_messages:
            body = message.removeprefix("**Assistant**\n")
            self.assertTrue(body.startswith("```python\n"))
            self.assertTrue(body.endswith("```"))

    async def test_approval_request_posts_compact_reactions(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_approval_request(
            RemoteApprovalRequest(
                approval_id="approval-1",
                call_id="call-1",
                turn_id="turn-1",
                session_id="session-1",
                session_epoch="epoch-1",
                command=["git", "status"],
                cwd="/repo",
                reason="Need approval",
            )
        )

        self.assertEqual(len(thread.sent_messages), 1)
        self.assertIn("Need approval", thread.sent_messages[0])
        self.assertIn("Quick review", thread.sent_messages[0])
        self.assertTrue(thread.sent_kwargs[0]["suppress_embeds"])
        self.assertIsNone(thread.sent_views[0])
        approval_message = await thread.fetch_message(901)
        self.assertEqual(approval_message.reactions, ["✅", "✖️"])

    async def test_continue_reaction_reuses_control_message_for_status_feedback(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        session = EveryCodeSession(hello=make_hello(), websocket=websocket, thread_id=555)
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_session_status(
            "turn_complete",
            SessionStatus(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Waiting for direction",
                assistant_message=None,
            ),
        )

        handled = await bridge.handle_thread_reaction(
            cast(Any, thread),
            901,
            "▶️",
            cast(Any, SimpleNamespace(id=123)),
        )

        self.assertTrue(handled)
        control_message = await thread.fetch_message(901)
        self.assertEqual(control_message.reactions, [bridge_module.REACTION_QUEUED])
        self.assertEqual(session.control_message_id, 901)
        self.assertEqual(websocket.sent_json[0]["kind"], "continue_autonomously")

    async def test_thread_reply_shows_active_control_anchor(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        control_message = add_bot_message(thread, 801, "\u200b")
        control_message.reactions = ["▶️", bridge_module.REACTION_CONTROL_STATUS, "⏹️"]
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=websocket,
            thread_id=555,
            control_message_id=801,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        reply_message = FakeReplyMessage(802, thread, "Run the quick path")
        thread.add_message(reply_message)

        delivered = await bridge.send_thread_reply(cast(Any, reply_message))

        self.assertTrue(delivered)
        self.assertEqual(reply_message.reactions, [bridge_module.REACTION_QUEUED])
        self.assertEqual(
            control_message.reactions,
            [
                bridge_module.REACTION_QUEUED,
                bridge_module.REACTION_CONTROL_PAUSE,
                bridge_module.REACTION_CONTROL_END,
            ],
        )
        self.assertEqual(session.control_message_id, 801)
        self.assertTrue(session.control_interruptions_enabled)
        self.assertEqual(websocket.sent_json[0]["kind"], "reply")

    async def test_thread_reply_ack_clears_delivery_receipt(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        control_message = add_bot_message(thread, 801, "\u200b")
        control_message.reactions = ["▶️", bridge_module.REACTION_CONTROL_STATUS, "⏹️"]
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=websocket,
            thread_id=555,
            control_message_id=801,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        reply_message = FakeReplyMessage(802, thread, "Run the quick path")
        thread.add_message(reply_message)

        await bridge.send_thread_reply(cast(Any, reply_message))
        command_id = next(iter(session.pending_commands))

        await bridge.handle_command_ack({"session_id": "session-1", "command_id": command_id})

        self.assertEqual(reply_message.reactions, [])
        self.assertEqual(session.active_command_id, command_id)
        self.assertEqual(
            control_message.reactions,
            [
                bridge_module.REACTION_QUEUED,
                bridge_module.REACTION_CONTROL_PAUSE,
                bridge_module.REACTION_CONTROL_END,
            ],
        )

    async def test_thread_reply_turn_complete_moves_controls_after_assistant(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        control_message = add_bot_message(thread, 801, "\u200b")
        control_message.reactions = ["▶️", bridge_module.REACTION_CONTROL_STATUS, "⏹️"]
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=websocket,
            thread_id=555,
            control_message_id=801,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        reply_message = FakeReplyMessage(802, thread, "Run the quick path")
        thread.add_message(reply_message)

        await bridge.send_thread_reply(cast(Any, reply_message))
        command_id = next(iter(session.pending_commands))
        session.active_command_id = command_id

        await bridge.handle_session_status(
            "turn_complete",
            SessionStatus(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Waiting for direction",
                assistant_message="Done.",
            ),
        )

        self.assertTrue(control_message.deleted)
        self.assertEqual(reply_message.reactions, [])
        self.assertEqual(
            thread.sent_messages,
            [
                "**Assistant**\nDone.",
                "\u200b",
            ],
        )
        new_control_message = await thread.fetch_message(902)
        self.assertEqual(new_control_message.reactions, ["▶️", bridge_module.REACTION_CONTROL_STATUS, "⏹️"])
        self.assertEqual(session.control_message_id, 902)
        self.assertFalse(session.control_interruptions_enabled)

    async def test_turn_complete_clears_recovered_rejected_reply_reaction(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        control_message = add_bot_message(thread, 801, "\u200b")
        control_message.reactions = ["▶️", bridge_module.REACTION_CONTROL_STATUS, "⏹️"]
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=websocket,
            thread_id=555,
            control_message_id=801,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        reply_message = FakeReplyMessage(802, thread, "Run the quick path")
        thread.add_message(reply_message)

        await bridge.send_thread_reply(cast(Any, reply_message))
        command_id = next(iter(session.pending_commands))
        await bridge.handle_command_reject({"session_id": "session-1", "command_id": command_id})

        self.assertEqual(reply_message.reactions, [bridge_module.REACTION_REJECTED])
        self.assertEqual(len(session.rejected_command_messages), 1)

        await bridge.handle_session_status(
            "turn_complete",
            SessionStatus(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Waiting for direction",
                assistant_message="Recovered.",
            ),
        )

        self.assertEqual(reply_message.reactions, [])
        self.assertEqual(session.rejected_command_messages, [])

    async def test_turn_complete_clears_stale_reactions_before_deleting_old_anchor(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        control_message = add_bot_message(thread, 801, "\u200b")
        control_message.reactions = [bridge_module.REACTION_REJECTED]
        control_message.delete_raises = True
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
            control_message_id=801,
            control_status_reaction=bridge_module.REACTION_REJECTED,
            control_interruptions_enabled=True,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_session_status(
            "turn_complete",
            SessionStatus(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Waiting for direction",
                assistant_message="Done.",
            ),
        )

        self.assertFalse(control_message.deleted)
        self.assertEqual(control_message.reactions, [])
        new_control_message = await thread.fetch_message(902)
        self.assertEqual(new_control_message.reactions, ["▶️", bridge_module.REACTION_CONTROL_STATUS, "⏹️"])
        self.assertEqual(session.control_message_id, 902)
        self.assertFalse(session.control_interruptions_enabled)

    async def test_turn_complete_restores_reaction_controls_on_existing_anchor(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        control_message = add_bot_message(thread, 901, "\u200b")
        control_message.reactions = [bridge_module.REACTION_IN_PROGRESS]
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=websocket,
            thread_id=555,
            control_message_id=901,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        session.pending_commands["cmd-1"] = sessions_module.PendingRemoteCommand(
            thread_id=555,
            message_id=901,
            kind="continue_autonomously",
        )
        session.active_command_id = "cmd-1"

        await bridge.handle_session_status(
            "turn_complete",
            SessionStatus(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Waiting for direction",
                assistant_message=None,
            ),
        )

        self.assertEqual(control_message.reactions, ["▶️", bridge_module.REACTION_CONTROL_STATUS, "⏹️"])
        self.assertEqual(session.control_message_id, 901)
        self.assertEqual(thread.sent_messages, [])

    async def test_status_changed_updates_existing_control_anchor_reaction(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        control_message = add_bot_message(thread, 901, "\u200b")
        control_message.reactions = [bridge_module.REACTION_QUEUED]
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=websocket,
            thread_id=555,
            control_message_id=901,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        session.pending_commands["cmd-1"] = sessions_module.PendingRemoteCommand(
            thread_id=555,
            message_id=901,
            kind="continue_autonomously",
        )
        session.active_command_id = "cmd-1"

        await bridge.handle_session_status(
            "status_changed",
            SessionStatus(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Turn started",
                assistant_message=None,
            ),
        )

        self.assertEqual(
            control_message.reactions,
            [
                bridge_module.REACTION_IN_PROGRESS,
                bridge_module.REACTION_CONTROL_PAUSE,
                bridge_module.REACTION_CONTROL_END,
            ],
        )
        self.assertFalse(control_message.deleted)

    async def test_pause_reaction_queues_remote_command(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        session = EveryCodeSession(hello=make_hello(), websocket=websocket, thread_id=555)
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_session_status(
            "turn_complete",
            SessionStatus(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Waiting for direction",
                assistant_message=None,
            ),
        )

        handled = await bridge.handle_thread_reaction(
            cast(Any, thread),
            901,
            "⏸️",
            cast(Any, SimpleNamespace(id=123)),
        )

        self.assertTrue(handled)
        control_message = await thread.fetch_message(901)
        self.assertEqual(control_message.reactions, [bridge_module.REACTION_QUEUED])
        self.assertEqual(websocket.sent_json[0]["kind"], "pause_current_turn")

    async def test_end_session_reaction_requires_confirmation(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        session = EveryCodeSession(hello=make_hello(), websocket=websocket, thread_id=555)
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_session_status(
            "turn_complete",
            SessionStatus(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Waiting for direction",
                assistant_message=None,
            ),
        )

        handled = await bridge.handle_thread_reaction(
            cast(Any, thread),
            901,
            "⏹️",
            cast(Any, SimpleNamespace(id=123)),
        )

        self.assertTrue(handled)
        control_message = await thread.fetch_message(901)
        self.assertEqual(control_message.reactions, ["✅", "✖️"])
        self.assertEqual(websocket.sent_json, [])

        handled = await bridge.handle_thread_reaction(
            cast(Any, thread),
            901,
            "✅",
            cast(Any, SimpleNamespace(id=123)),
        )

        self.assertTrue(handled)
        self.assertEqual(control_message.reactions, [bridge_module.REACTION_QUEUED])
        self.assertEqual(websocket.sent_json[0]["kind"], "end_session")

    async def test_end_session_confirmation_cancel_restores_controls(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        session = EveryCodeSession(hello=make_hello(), websocket=websocket, thread_id=555)
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_session_status(
            "turn_complete",
            SessionStatus(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Waiting for direction",
                assistant_message=None,
            ),
        )

        await bridge.handle_thread_reaction(
            cast(Any, thread),
            901,
            "⏹️",
            cast(Any, SimpleNamespace(id=123)),
        )

        handled = await bridge.handle_thread_reaction(
            cast(Any, thread),
            901,
            "✖️",
            cast(Any, SimpleNamespace(id=123)),
        )

        self.assertTrue(handled)
        control_message = await thread.fetch_message(901)
        self.assertEqual(control_message.reactions, ["▶️", bridge_module.REACTION_CONTROL_STATUS, "⏹️"])
        self.assertEqual(websocket.sent_json, [])

    async def test_approval_reaction_edits_message_pending_state(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        session = EveryCodeSession(hello=make_hello(), websocket=websocket, thread_id=555)
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_approval_request(
            RemoteApprovalRequest(
                approval_id="approval-1",
                call_id="call-1",
                turn_id="turn-1",
                session_id="session-1",
                session_epoch="epoch-1",
                command=["git", "status"],
                cwd="/repo",
                reason="Need approval",
            )
        )

        handled = await bridge.handle_thread_reaction(
            cast(Any, thread),
            901,
            "✅",
            cast(Any, SimpleNamespace(id=123)),
        )

        self.assertTrue(handled)
        approval_message = await thread.fetch_message(901)
        self.assertIn("Approval sent", approval_message.content)
        self.assertEqual(approval_message.reactions, [])
        self.assertNotIn("suppress", approval_message.edit_kwargs[-1])
        self.assertEqual(websocket.sent_json[0]["decision"], "approved")

    async def test_approval_interaction_does_not_suppress_embeds_on_edit(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        session = EveryCodeSession(hello=make_hello(), websocket=websocket, thread_id=555)
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        session.pending_approvals["approval-1"] = PendingRemoteApproval(thread_id=555, message_id=901)
        interaction = FakeInteraction(thread)

        await bridge.handle_approval_interaction(
            cast(Any, interaction),
            "session-1",
            "approval-1",
            "approved",
        )

        self.assertIn("Approval sent", interaction.response.edits[0][0])
        self.assertNotIn("suppress_embeds", interaction.response.edit_kwargs[0])
        self.assertEqual(websocket.sent_json[0]["decision"], "approved")

    async def test_approval_decision_ack_marks_message_finished(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        approval_message = FakeReplyMessage(901, thread, "**Approval sent**")
        thread.add_message(approval_message)
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
        )
        session.pending_approvals["approval-1"] = sessions_module.PendingRemoteApproval(
            thread_id=555,
            message_id=901,
            decision="approved",
            decided_by=123,
        )
        bridge.sessions.register(session)

        await bridge.handle_approval_decision_ack(
            {
                "session_id": "session-1",
                "approval_id": "approval-1",
            }
        )

        self.assertEqual(approval_message.content, "**Approved**\nby: `123`")
        self.assertEqual(approval_message.reactions, [])
        self.assertEqual(session.pending_approvals, {})

    async def test_approval_decision_reject_marks_message_expired(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        approval_message = FakeReplyMessage(901, thread, "**Approval sent**")
        thread.add_message(approval_message)
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
        )
        session.pending_approvals["approval-1"] = sessions_module.PendingRemoteApproval(
            thread_id=555,
            message_id=901,
            decision="approved",
            decided_by=123,
        )
        bridge.sessions.register(session)

        await bridge.handle_approval_decision_reject(
            {
                "session_id": "session-1",
                "approval_id": "approval-1",
                "reason": "approval timed out",
            }
        )

        self.assertEqual(approval_message.content, "**Approval expired**\napproval timed out")
        self.assertEqual(approval_message.reactions, [])
        self.assertEqual(session.pending_approvals, {})

    async def test_request_user_input_posts_select_prompt(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_request_user_input(
            RemoteRequestUserInput(
                call_id="call-1",
                turn_id="turn-1",
                session_id="session-1",
                session_epoch="epoch-1",
                questions=[
                    RequestUserInputQuestion(
                        id="mode",
                        header="Build mode",
                        question="Choose a mode",
                        is_other=False,
                        is_secret=False,
                        options=[
                            RequestUserInputQuestionOption(
                                label="Fast",
                                description="Skip extra checks",
                            ),
                            RequestUserInputQuestionOption(
                                label="Safe",
                                description="Run the full path",
                            ),
                        ],
                    )
                ],
            )
        )

        self.assertEqual(len(thread.sent_messages), 1)
        self.assertIn("Build mode", thread.sent_messages[0])
        self.assertIn("Choose a mode", thread.sent_messages[0])
        self.assertIsNotNone(thread.sent_views[0])
        children = list(cast(Any, thread.sent_views[0]).children)
        selects = [child for child in children if isinstance(child, bridge_module.discord.ui.Select)]
        buttons = [child for child in children if isinstance(child, bridge_module.discord.ui.Button)]
        self.assertEqual(len(selects), 1)
        self.assertEqual([option.label for option in selects[0].options], ["Fast", "Safe"])
        self.assertEqual([button.label for button in buttons], ["Submit", "Cancel"])
        self.assertIn("turn-1", session.pending_user_inputs)

    async def test_request_user_input_modal_prompt_uses_buttons(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_request_user_input(
            RemoteRequestUserInput(
                call_id="call-1",
                turn_id="turn-2",
                session_id="session-1",
                session_epoch="epoch-1",
                questions=[
                    RequestUserInputQuestion(
                        id="summary",
                        header="Summary",
                        question="What should we tell the user?",
                        is_other=True,
                        is_secret=False,
                        options=[],
                    ),
                    RequestUserInputQuestion(
                        id="token",
                        header="Token",
                        question="Provide a secret token",
                        is_other=True,
                        is_secret=True,
                        options=[],
                    ),
                ],
            )
        )

        children = list(cast(Any, thread.sent_views[0]).children)
        self.assertEqual(len(children), 4)
        buttons = [child for child in children if isinstance(child, bridge_module.discord.ui.Button)]
        self.assertEqual([button.label for button in buttons], ["Summary", "Token", "Submit", "Cancel"])
        self.assertIn("Use the controls below", thread.sent_messages[0])

    async def test_request_user_input_submit_sends_call_id(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        websocket = FakeWebSocket()
        bridge = EveryCodeBridge(FakeBot(config, thread))
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=websocket,
            thread_id=555,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        request = RemoteRequestUserInput(
            call_id="call-1",
            turn_id="turn-1",
            session_id="session-1",
            session_epoch="epoch-1",
            questions=[
                RequestUserInputQuestion(
                    id="mode",
                    header="Build mode",
                    question="Choose a mode",
                    is_other=False,
                    is_secret=False,
                    options=[
                        RequestUserInputQuestionOption(
                            label="Safe",
                            description="Run the full path",
                        ),
                    ],
                )
            ],
        )
        await bridge.handle_request_user_input(request)

        view = cast(Any, thread.sent_views[0])
        view.set_answer("mode", "Safe")
        interaction = FakeInteraction(thread)
        await view.submit(interaction)

        self.assertEqual(websocket.sent_json[0]["call_id"], "call-1")
        self.assertEqual(websocket.sent_json[0]["turn_id"], "turn-1")
        self.assertEqual(
            websocket.sent_json[0]["response"],
            {"answers": {"mode": {"answers": ["Safe"]}}},
        )
        self.assertIn("Answer sent", interaction.response.edits[0][0])
        self.assertNotIn("suppress_embeds", interaction.response.edit_kwargs[0])

    async def test_request_user_input_cancel_sends_empty_response(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        websocket = FakeWebSocket()
        bridge = EveryCodeBridge(FakeBot(config, thread))
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=websocket,
            thread_id=555,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        request = RemoteRequestUserInput(
            call_id="call-1",
            turn_id="turn-1",
            session_id="session-1",
            session_epoch="epoch-1",
            questions=[
                RequestUserInputQuestion(
                    id="mode",
                    header="Build mode",
                    question="Choose a mode",
                    is_other=False,
                    is_secret=False,
                    options=[
                        RequestUserInputQuestionOption(
                            label="Safe",
                            description="Run the full path",
                        ),
                    ],
                )
            ],
        )
        await bridge.handle_request_user_input(request)

        view = cast(Any, thread.sent_views[0])
        interaction = FakeInteraction(thread)
        await view.cancel(interaction)

        self.assertEqual(websocket.sent_json[0]["kind"], "request_user_input_response")
        self.assertEqual(websocket.sent_json[0]["call_id"], "call-1")
        self.assertEqual(websocket.sent_json[0]["turn_id"], "turn-1")
        self.assertEqual(websocket.sent_json[0]["response"], {"answers": {}})
        self.assertIn("Answer cancelled", interaction.response.edits[0][0])
        self.assertNotIn("suppress_embeds", interaction.response.edit_kwargs[0])

    async def test_status_changed_preserves_contextual_controls_for_active_reply_command(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        control_message = FakeReplyMessage(901, thread, "Waiting")
        reply_message = FakeReplyMessage(902, thread, "Continue")
        thread.add_message(control_message)
        thread.add_message(reply_message)
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
            control_message_id=901,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        session.pending_commands["cmd-1"] = sessions_module.PendingRemoteCommand(
            thread_id=555,
            message_id=902,
            kind="reply",
        )
        session.active_command_id = "cmd-1"

        await bridge.handle_session_status(
            "status_changed",
            SessionStatus(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Turn started",
                assistant_message=None,
            ),
        )

        self.assertFalse(control_message.deleted)
        self.assertEqual(session.control_message_id, 901)
        self.assertEqual(reply_message.reactions, [])
        self.assertEqual(
            control_message.reactions,
            [
                bridge_module.REACTION_IN_PROGRESS,
                bridge_module.REACTION_CONTROL_PAUSE,
                bridge_module.REACTION_CONTROL_END,
            ],
        )

    async def test_status_changed_uses_existing_control_anchor_without_active_command(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        control_message = FakeReplyMessage(901, thread, "Waiting")
        control_message.reactions = ["▶️", bridge_module.REACTION_CONTROL_STATUS, "⏹️"]
        thread.add_message(control_message)
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
            control_message_id=901,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_session_status(
            "status_changed",
            SessionStatus(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Compacting conversation history",
                assistant_message=None,
            ),
        )

        self.assertFalse(control_message.deleted)
        self.assertEqual(control_message.reactions, [bridge_module.REACTION_COMPACTING])
        self.assertEqual(session.control_message_id, 901)
        self.assertEqual(session.control_status_reaction, bridge_module.REACTION_COMPACTING)

    async def test_handle_user_message_formats_distinct_notice(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_user_message(
            protocol_module.UserMessage(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Run the quick path",
            )
        )

        self.assertEqual(
            thread.sent_messages,
            [
                "**You**\n>>> Run the quick path",
                "\u200b",
            ],
        )
        control_message = await thread.fetch_message(902)
        self.assertEqual(
            control_message.reactions,
            [
                bridge_module.REACTION_IN_PROGRESS,
                bridge_module.REACTION_CONTROL_PAUSE,
                bridge_module.REACTION_CONTROL_END,
            ],
        )
        self.assertEqual(session.control_message_id, 902)
        self.assertTrue(session.control_interruptions_enabled)

    async def test_tui_user_message_turn_complete_moves_controls_after_assistant(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_user_message(
            protocol_module.UserMessage(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Run the quick path",
            )
        )
        old_control_message = await thread.fetch_message(902)

        await bridge.handle_session_status(
            "turn_complete",
            SessionStatus(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Waiting for direction",
                assistant_message="Done.",
            ),
        )

        self.assertTrue(old_control_message.deleted)
        self.assertEqual(
            thread.sent_messages,
            [
                "**You**\n>>> Run the quick path",
                "\u200b",
                "**Assistant**\nDone.",
                "\u200b",
            ],
        )
        control_message = await thread.fetch_message(904)
        self.assertEqual(control_message.reactions, ["▶️", bridge_module.REACTION_CONTROL_STATUS, "⏹️"])
        self.assertEqual(session.control_message_id, 904)
        self.assertFalse(session.control_interruptions_enabled)

    async def test_active_sessions_summary_lists_live_sessions(self) -> None:
        config = Config()
        bridge = EveryCodeBridge(FakeBot(config))
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        self.assertEqual(
            bridge.active_sessions_summary(),
            "\n".join(
                [
                    "Live Every Code sessions:",
                    "- `project · sess` (online, Mac Studio) <#555>",
                ]
            ),
        )

    async def test_active_sessions_summary_handles_empty_registry(self) -> None:
        config = Config()
        bridge = EveryCodeBridge(FakeBot(config))

        self.assertEqual(bridge.active_sessions_summary(), "No live Every Code sessions.")

    async def test_active_sessions_summary_marks_every_code_origin(self) -> None:
        config = Config()
        bridge = EveryCodeBridge(FakeBot(config))
        hello = SessionHello(
            session_id="session-1",
            session_epoch="epoch-1",
            host_label="Mac Studio",
            cwd="/tmp/project",
            branch="main",
            pid=42,
            origin=SessionOrigin(
                kind="every_code",
                request_id="every-code-cbusillo-syo-67",
                repository="cbusillo/sellyouroutboard",
                issue_number=67,
                issue_url="https://github.com/cbusillo/sellyouroutboard/issues/67",
            ),
        )
        session = EveryCodeSession(
            hello=hello,
            websocket=FakeWebSocket(),
            thread_id=555,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        self.assertEqual(
            bridge.active_sessions_summary(),
            "\n".join(
                [
                    "Live Every Code sessions:",
                    "- `EC sellyouroutboard#67` (online, Mac Studio) <#555>",
                ]
            ),
        )

    async def test_session_status_summary_uses_last_status(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_session_status(
            "status_changed",
            SessionStatus(
                session_id="session-1",
                session_epoch="epoch-1",
                message="Turn started",
                assistant_message=None,
            ),
        )

        self.assertEqual(
            bridge.session_status_summary(thread, SimpleNamespace(id=123)),
            "\n".join(
                [
                    "Every Code `project · sess`",
                    "state: online",
                    "host: Mac Studio",
                    "status: Turn started",
                ]
            ),
        )

    async def test_session_status_summary_marks_every_code_origin(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        hello = SessionHello(
            session_id="session-1",
            session_epoch="epoch-1",
            host_label="Mac Studio",
            cwd="/tmp/project",
            branch="main",
            pid=42,
            origin=SessionOrigin(
                kind="every_code",
                request_id="every-code-cbusillo-syo-67",
                repository="cbusillo/sellyouroutboard",
                issue_number=67,
                issue_url="https://github.com/cbusillo/sellyouroutboard/issues/67",
            ),
        )
        session = EveryCodeSession(
            hello=hello,
            websocket=FakeWebSocket(),
            thread_id=555,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        self.assertEqual(
            bridge.session_status_summary(thread, SimpleNamespace(id=123)),
            "\n".join(
                [
                    "Every Code `EC sellyouroutboard#67`",
                    "state: online",
                    "host: Mac Studio",
                    "status: No status update received yet.",
                ]
            ),
        )

    async def test_session_metadata_changed_renames_human_thread_to_working_branch(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_session_metadata_changed(
            SessionMetadataChanged(
                session_id="session-1",
                session_epoch="epoch-1",
                cwd="/tmp/project-worktree",
                branch="code/project-task",
                reason="working_branch_selected",
            )
        )

        self.assertEqual(session.hello.cwd, "/tmp/project")
        self.assertEqual(session.hello.branch, "code/project-task")
        self.assertEqual(thread.name, "project · code/project-task · sess")
        self.assertEqual(thread.edits[-1]["reason"], "Every Code working branch selected")

    async def test_session_metadata_changed_preserves_missing_branch(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_session_metadata_changed(
            SessionMetadataChanged(
                session_id="session-1",
                session_epoch="epoch-1",
                cwd="/tmp/project-worktree",
                branch=None,
                reason="working_branch_selected",
            )
        )

        self.assertEqual(session.hello.cwd, "/tmp/project")
        self.assertEqual(session.hello.branch, "main")
        self.assertIsNone(thread.name)
        self.assertEqual(thread.edits, [])

    async def test_session_metadata_changed_fetches_uncached_thread_before_renaming(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bot = FetchOnlyFakeBot(config, thread)
        bridge = EveryCodeBridge(bot)
        session = EveryCodeSession(
            hello=make_hello(),
            websocket=FakeWebSocket(),
            thread_id=555,
        )
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_session_metadata_changed(
            SessionMetadataChanged(
                session_id="session-1",
                session_epoch="epoch-1",
                cwd="/tmp/project-worktree",
                branch="code/project-task",
                reason="working_branch_selected",
            )
        )

        self.assertEqual(bot.fetch_channel_calls, [555])
        self.assertEqual(thread.name, "project · code/project-task · sess")

    async def test_session_metadata_changed_skips_every_code_origin_rename(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        hello = SessionHello(
            session_id="session-1",
            session_epoch="epoch-1",
            host_label="Mac Studio",
            cwd="/tmp/project",
            branch="main",
            pid=42,
            origin=SessionOrigin(
                kind="every_code",
                request_id="every-code-cbusillo-syo-67",
                repository="cbusillo/sellyouroutboard",
                issue_number=67,
                issue_url="https://github.com/cbusillo/sellyouroutboard/issues/67",
            ),
        )
        session = EveryCodeSession(hello=hello, websocket=FakeWebSocket(), thread_id=555)
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)

        await bridge.handle_session_metadata_changed(
            SessionMetadataChanged(
                session_id="session-1",
                session_epoch="epoch-1",
                cwd="/tmp/project-worktree",
                branch="code/project-task",
                reason="working_branch_selected",
            )
        )

        self.assertEqual(session.hello.branch, "code/project-task")
        self.assertEqual(thread.edits, [])

    async def test_reconnect_reuses_matching_thread_with_assistant_history(self) -> None:
        config = Config()
        config.every_code.channel_id = 321
        hello = make_hello()
        empty_reconnect_thread = FakeThread(556)
        add_bot_message(empty_reconnect_thread, 1, session_start_message(hello))
        original_thread = FakeThread(555, archived=True, locked=True)
        add_bot_message(original_thread, 2, session_start_message(hello))
        add_bot_message(original_thread, 3, "**Assistant**\nLast useful answer")
        channel = FakeTextChannel(321, [empty_reconnect_thread, original_thread])
        bridge = EveryCodeBridge(FakeBot(config, channel=channel))

        session_thread = await bridge.find_or_create_session_thread(hello)

        self.assertIs(session_thread.thread, original_thread)
        self.assertIsNone(session_thread.notification_message_id)
        self.assertFalse(original_thread.archived)
        self.assertFalse(original_thread.locked)
        self.assertEqual(
            original_thread.edits[0]["reason"],
            "Reattaching live Every Code session after bridge restart",
        )

    async def test_same_session_reconnect_reuses_current_thread(self) -> None:
        config = Config()
        config.every_code.channel_id = 321
        hello = make_hello()
        original_thread = FakeThread(555)
        add_bot_message(original_thread, 1, session_start_message(hello))
        add_bot_message(original_thread, 2, "**Assistant**\nLast useful answer")
        channel = FakeTextChannel(321, [original_thread])
        bridge = EveryCodeBridge(FakeBot(config, channel=channel))

        old_session = EveryCodeSession(
            hello=hello,
            websocket=FakeWebSocket(),
            thread_id=original_thread.id,
        )
        bridge.sessions.register(old_session)
        new_session = EveryCodeSession(hello=hello, websocket=FakeWebSocket())
        bridge.sessions.register(new_session)

        session_thread = await bridge.find_or_create_session_thread(hello)
        bridge.sessions.bind_thread(
            hello.session_id,
            session_thread.thread.id,
            session_thread.notification_message_id,
        )

        self.assertIs(session_thread.thread, original_thread)
        self.assertIsNone(bridge.sessions.remove_if_current(old_session))
        self.assertIs(bridge.sessions.get(hello.session_id), new_session)

    async def test_reconnect_ignores_thread_for_different_session_metadata(self) -> None:
        config = Config()
        config.every_code.channel_id = 321
        hello = make_hello()
        other_thread = FakeThread(555)
        other_hello = SessionHello(
            session_id="session-2",
            session_epoch="epoch-1",
            host_label="Mac Studio",
            cwd="/tmp/other-project",
            branch="main",
            pid=42,
        )
        add_bot_message(other_thread, 1, session_start_message(other_hello))
        channel = FakeTextChannel(321, [other_thread])
        bridge = EveryCodeBridge(FakeBot(config, channel=channel))

        self.assertIsNone(await bridge.find_existing_session_thread(hello))

    async def test_backfill_posts_latest_assistant_when_thread_has_none(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        stub_recovered_assistant_message(bridge, "Recovered answer")

        await bridge.backfill_latest_assistant_message(thread, make_hello())

        self.assertEqual(thread.sent_messages, ["**Assistant**\nRecovered answer"])
        self.assertTrue(thread.sent_kwargs[0]["suppress_embeds"])

    async def test_backfill_skips_thread_with_existing_assistant(self) -> None:
        config = Config()
        thread = FakeThread(555)
        add_bot_message(thread, 1, "**Assistant**\nAlready present")
        bridge = EveryCodeBridge(FakeBot(config, thread))
        stub_recovered_assistant_message(bridge, "Recovered answer")

        await bridge.backfill_latest_assistant_message(thread, make_hello())

        self.assertEqual(thread.sent_messages, [])

    def test_latest_assistant_message_from_rollout_reads_last_agent_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            rollout.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "payload": {
                                    "msg": {
                                        "type": "agent_message",
                                        "message": "First",
                                    }
                                }
                            }
                        ),
                        json.dumps(
                            {
                                "payload": {
                                    "msg": {
                                        "type": "agent_message",
                                        "message": "Second",
                                    }
                                }
                            }
                        ),
                    ]
                )
            )

            self.assertEqual(
                EveryCodeBridge.latest_assistant_message_from_rollout(rollout),
                "Second",
            )


if __name__ == "__main__":
    unittest.main()
