from __future__ import annotations

import json
import os
import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, AsyncIterator, TypeAlias

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
SessionStatus = protocol_module.SessionStatus
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
        self.edits: list[tuple[str, bool]] = []

    async def add_reaction(self, reaction: str) -> None:
        self.reactions.append(reaction)

    async def remove_reaction(self, reaction: str, _user: object) -> None:
        if reaction in self.reactions:
            self.reactions.remove(reaction)

    async def reply(self, content: str, *, mention_author: bool) -> None:
        del mention_author
        self.replies.append(content)

    async def edit(self, content: str, **kwargs: object) -> None:
        self.content = content
        self.edits.append((content, kwargs.get("view") is None))


class FakeThread:
    def __init__(self, thread_id: int, *, archived: bool = False, locked: bool = False) -> None:
        self.id = thread_id
        self.archived = archived
        self.locked = locked
        self._messages: dict[int, FakeReplyMessage] = {}
        self._history: list[FakeReplyMessage] = []
        self.sent_messages: list[str] = []
        self.sent_views: list[object] = []
        self.edits: list[dict[str, object]] = []

    def add_message(self, message: FakeReplyMessage) -> None:
        self._messages[message.id] = message
        self._history.append(message)

    async def fetch_message(self, message_id: int) -> FakeReplyMessage:
        return self._messages[message_id]

    async def history(
        self,
        limit: int,
        oldest_first: bool = False,
    ) -> AsyncIterator[FakeReplyMessage]:
        messages = self._history[:limit] if oldest_first else list(reversed(self._history))[:limit]
        for message in messages:
            yield message

    async def send(self, content: str, **kwargs: object) -> FakeReplyMessage:
        self.sent_messages.append(content)
        self.sent_views.append(kwargs.get("view"))
        message = FakeReplyMessage(900 + len(self.sent_messages), self, content)
        self.add_message(message)
        return message

    async def edit(self, **kwargs: object) -> None:
        if "archived" in kwargs:
            self.archived = bool(kwargs["archived"])
        if "locked" in kwargs:
            self.locked = bool(kwargs["locked"])
        self.edits.append(kwargs)


class FakeTextChannel:
    def __init__(self, channel_id: int, threads: list[FakeThread]) -> None:
        self.id = channel_id
        self.threads = [thread for thread in threads if not thread.archived]
        self._archived_threads = [thread for thread in threads if thread.archived]

    async def archived_threads(self, **_: object) -> AsyncIterator[FakeThread]:
        for thread in self._archived_threads:
            yield thread


class FakeInteractionResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send_message(self, content: str, *, ephemeral: bool) -> None:
        self.messages.append((content, ephemeral))


class FakeInteraction:
    def __init__(
        self,
        channel: FakeThread,
        user_id: int = 123,
        message: FakeReplyMessage | None = None,
    ) -> None:
        self.channel = channel
        self.user = SimpleNamespace(id=user_id)
        self.message = message
        self.response = FakeInteractionResponse()


class FakeBot:
    def __init__(
        self,
        config: ConfigType,
        thread: FakeThread | None = None,
        channel: FakeTextChannel | None = None,
    ) -> None:
        self.config = config
        self.user = SimpleNamespace(id=999)
        self._thread = thread
        self._channel = channel

    def get_channel(self, channel_id: int) -> FakeThread | FakeTextChannel | None:
        if self._thread is not None and self._thread.id == channel_id:
            return self._thread
        if self._channel is not None and self._channel.id == channel_id:
            return self._channel
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


def add_bot_message(thread: FakeThread, message_id: int, content: str) -> FakeReplyMessage:
    message = FakeReplyMessage(message_id, thread, content)
    message.author = SimpleNamespace(id=999)
    thread.add_message(message)
    return message


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

    async def test_go_ahead_interaction_clears_contextual_controls(self) -> None:
        config = Config()
        config.discord.employee_role_name = ""
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        websocket = FakeWebSocket()
        session = EveryCodeSession(hello=make_hello(), websocket=websocket, thread_id=555)
        bridge.sessions.register(session)
        bridge.sessions.bind_thread("session-1", 555)
        control_message = FakeReplyMessage(901, thread, "Every Code `project` on `main`")
        thread.add_message(control_message)
        interaction = FakeInteraction(thread, message=control_message)

        await bridge.handle_go_ahead_interaction(interaction)

        self.assertEqual(control_message.content, "Every Code is continuing.")
        self.assertEqual(control_message.edits, [("Every Code is continuing.", True)])
        self.assertIsNone(session.control_message_id)
        self.assertEqual(websocket.sent_json[0]["kind"], "continue_autonomously")

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
                "Every Code `project` on `main`\nWaiting for direction",
            ],
        )
        self.assertIsNone(thread.sent_views[0])
        self.assertIsNotNone(thread.sent_views[1])
        self.assertEqual(session.control_message_id, 902)

    async def test_status_changed_clears_contextual_controls(self) -> None:
        config = Config()
        thread = FakeThread(555)
        bridge = EveryCodeBridge(FakeBot(config, thread))
        control_message = FakeReplyMessage(901, thread, "Waiting")
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
                message="Turn started",
                assistant_message=None,
            ),
        )

        self.assertEqual(control_message.content, "Every Code is working.")
        self.assertIsNone(session.control_message_id)

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
                    "- `project` on `main` (online, Mac Studio) <#555>",
                ]
            ),
        )

    async def test_active_sessions_summary_handles_empty_registry(self) -> None:
        config = Config()
        bridge = EveryCodeBridge(FakeBot(config))

        self.assertEqual(bridge.active_sessions_summary(), "No live Every Code sessions.")

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
                    "Every Code `project` on `main`",
                    "state: online",
                    "host: Mac Studio",
                    "status: Turn started",
                ]
            ),
        )

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
        bridge.recover_latest_assistant_message = lambda _hello: "Recovered answer"  # type: ignore[method-assign]

        await bridge.backfill_latest_assistant_message(thread, make_hello())

        self.assertEqual(thread.sent_messages, ["**Assistant**\nRecovered answer"])

    async def test_backfill_skips_thread_with_existing_assistant(self) -> None:
        config = Config()
        thread = FakeThread(555)
        add_bot_message(thread, 1, "**Assistant**\nAlready present")
        bridge = EveryCodeBridge(FakeBot(config, thread))
        bridge.recover_latest_assistant_message = lambda _hello: "Recovered answer"  # type: ignore[method-assign]

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
