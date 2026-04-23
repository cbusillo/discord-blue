from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from aiohttp import web

from discord_blue.doodads.every_code.protocol import SessionHello


@dataclass(slots=True)
class PendingRemoteCommand:
    thread_id: int
    message_id: int | None
    notify_on_reject: bool = False


@dataclass(slots=True)
class PendingRemoteApproval:
    thread_id: int
    message_id: int
    decision: str | None = None
    decided_by: int | None = None


@dataclass(slots=True)
class EveryCodeSession:
    hello: SessionHello
    websocket: web.WebSocketResponse
    thread_id: int | None = None
    notification_message_id: int | None = None
    control_message_id: int | None = None
    last_seen: datetime = field(default_factory=lambda: datetime.now(UTC))
    pending_commands: dict[str, PendingRemoteCommand] = field(default_factory=dict)
    pending_approvals: dict[str, PendingRemoteApproval] = field(default_factory=dict)
    active_command_id: str | None = None
    last_status_message: str | None = None

    @property
    def session_id(self) -> str:
        return self.hello.session_id

    @property
    def session_epoch(self) -> str:
        return self.hello.session_epoch

    def touch(self) -> None:
        self.last_seen = datetime.now(UTC)


class EveryCodeSessionRegistry:
    def __init__(self) -> None:
        self.by_session: dict[str, EveryCodeSession] = {}
        self.by_thread: dict[int, str] = {}

    def register(self, session: EveryCodeSession) -> None:
        self.by_session[session.session_id] = session
        if session.thread_id is not None:
            self.by_thread[session.thread_id] = session.session_id

    def bind_thread(
        self,
        session_id: str,
        thread_id: int,
        notification_message_id: int | None = None,
    ) -> None:
        if session := self.by_session.get(session_id):
            for existing_thread_id, existing_session_id in list(self.by_thread.items()):
                if existing_session_id == session_id:
                    self.by_thread.pop(existing_thread_id, None)
            session.thread_id = thread_id
            session.notification_message_id = notification_message_id
            self.by_thread[thread_id] = session_id

    def get_by_thread(self, thread_id: int) -> EveryCodeSession | None:
        session_id = self.by_thread.get(thread_id)
        if session_id is None:
            return None
        return self.by_session.get(session_id)

    def get(self, session_id: str) -> EveryCodeSession | None:
        return self.by_session.get(session_id)

    def remove(self, session_id: str) -> EveryCodeSession | None:
        session = self.by_session.pop(session_id, None)
        if session and session.thread_id is not None:
            self.by_thread.pop(session.thread_id, None)
        return session

    def remove_if_current(self, session: EveryCodeSession) -> EveryCodeSession | None:
        current = self.by_session.get(session.session_id)
        if current is not session:
            return None
        return self.remove(session.session_id)
