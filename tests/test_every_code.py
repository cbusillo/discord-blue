from __future__ import annotations

import os
import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from discord_blue.config import Config as ConfigType
    from discord_blue.doodads.every_code.protocol import SessionHello as SessionHelloType
else:
    ConfigType: TypeAlias = object
    SessionHelloType: TypeAlias = object

_TEST_HOME = tempfile.TemporaryDirectory()
os.environ["HOME"] = _TEST_HOME.name
_CONFIG_PATH = Path(_TEST_HOME.name) / ".config" / "discord-blue" / "config.toml"
_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
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

Config = importlib.import_module("discord_blue.config").Config
bridge_module = importlib.import_module("discord_blue.doodads.every_code.bridge")
EveryCodeBridge = bridge_module.EveryCodeBridge
protocol_module = importlib.import_module("discord_blue.doodads.every_code.protocol")
RemoteCommand = protocol_module.RemoteCommand
SessionHello = protocol_module.SessionHello
sessions_module = importlib.import_module("discord_blue.doodads.every_code.sessions")
EveryCodeSession = sessions_module.EveryCodeSession
EveryCodeSessionRegistry = sessions_module.EveryCodeSessionRegistry
threads_module = importlib.import_module("discord_blue.doodads.every_code.threads")
session_notification_message = threads_module.session_notification_message
session_start_message = threads_module.session_start_message
session_thread_name = threads_module.session_thread_name


class FakeWebSocket:
    def __init__(self, *, closed: bool = False) -> None:
        self.closed = closed
        self.sent_json: list[dict[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent_json.append(payload)


class FakeReplyMessage:
    def __init__(self, message_id: int, channel: FakeThread, content: str = "") -> None:
        self.id = message_id
        self.channel = channel
        self.content = content
        self.author = SimpleNamespace(id=123)
        self.reactions: list[str] = []
        self.replies: list[str] = []

    async def add_reaction(self, reaction: str) -> None:
        self.reactions.append(reaction)

    async def remove_reaction(self, reaction: str, _user: object) -> None:
        if reaction in self.reactions:
            self.reactions.remove(reaction)

    async def reply(self, content: str, *, mention_author: bool) -> None:
        del mention_author
        self.replies.append(content)


class FakeThread:
    def __init__(self, thread_id: int) -> None:
        self.id = thread_id
        self._messages: dict[int, FakeReplyMessage] = {}
        self.sent_messages: list[str] = []

    def add_message(self, message: FakeReplyMessage) -> None:
        self._messages[message.id] = message

    async def fetch_message(self, message_id: int) -> FakeReplyMessage:
        return self._messages[message_id]

    async def send(self, content: str, **_: object) -> SimpleNamespace:
        self.sent_messages.append(content)
        return SimpleNamespace(id=900 + len(self.sent_messages))


class FakeInteractionResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send_message(self, content: str, *, ephemeral: bool) -> None:
        self.messages.append((content, ephemeral))


class FakeInteraction:
    def __init__(self, channel: FakeThread, user_id: int = 123) -> None:
        self.channel = channel
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeInteractionResponse()


class FakeBot:
    def __init__(self, config: ConfigType, thread: FakeThread | None = None) -> None:
        self.config = config
        self.user = SimpleNamespace(id=999)
        self._thread = thread

    def get_channel(self, thread_id: int) -> FakeThread | None:
        if self._thread is not None and self._thread.id == thread_id:
            return self._thread
        return None


def make_hello() -> SessionHelloType:
    return SessionHello(
        session_id="session-1",
        session_epoch="epoch-1",
        host_label="Mac Studio",
        cwd="/tmp/project",
        branch="main",
        pid=42,
    )


class ConfigTests(unittest.TestCase):
    def test_every_code_config_loads_and_saves(self) -> None:
        _CONFIG_PATH.write_text(
            "\n".join(
                [
                    "[discord]",
                    'token = "from-file"',
                    "guild_id = 1",
                    "bot_channel_id = 2",
                    'employee_role_name = "employee"',
                    'loaded_doodads = ["every_code"]',
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


class ThreadFormattingTests(unittest.TestCase):
    def test_session_thread_text_uses_repo_branch_and_no_mentions(self) -> None:
        hello = make_hello()
        thread = SimpleNamespace(id=555)

        self.assertEqual(session_thread_name(hello), "project · main")
        self.assertEqual(
            session_notification_message(hello, thread),
            "Every Code session connected for `project` on `main`: <#555>",
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

        self.assertEqual(session_thread_name(hello), "session")
        self.assertEqual(
            session_notification_message(hello, thread),
            "Every Code session connected for `session`: <#555>",
        )
        self.assertIn("branch: `unknown`", session_start_message(hello))


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_thread_type = bridge_module.discord.Thread
        bridge_module.discord.Thread = FakeThread

    async def asyncTearDown(self) -> None:
        bridge_module.discord.Thread = self.original_thread_type

    async def test_websocket_auth_rejects_missing_or_wrong_token(self) -> None:
        config = Config()
        config.every_code.token = "shared-secret"
        bridge = EveryCodeBridge(FakeBot(config))

        self.assertFalse(bridge._authorized(SimpleNamespace(headers={})))
        self.assertFalse(
            bridge._authorized(SimpleNamespace(headers={"Authorization": "Bearer wrong"}))
        )
        self.assertTrue(
            bridge._authorized(SimpleNamespace(headers={"Authorization": "Bearer shared-secret"}))
        )

    async def test_thread_reply_routes_to_registered_session_websocket(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        session = EveryCodeSession(hello=make_hello(), websocket=websocket, thread_id=555)
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
        self.assertTrue(pending.notify_on_reject)

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


if __name__ == "__main__":
    unittest.main()
