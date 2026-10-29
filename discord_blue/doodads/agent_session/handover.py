from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import math
from typing import Any, TYPE_CHECKING

from discord_blue.doodads.agent_session.sessions import AgentSession

if TYPE_CHECKING:
    from discord_blue.doodads.agent_session.bridge import AgentSessionBridge


class SessionContinuation:
    def __init__(self, bridge: AgentSessionBridge, grace_seconds: float) -> None:
        self.bridge = bridge
        self.grace_seconds = grace_seconds
        self.saved: dict[str, Any] = {}
        self.changed = asyncio.Condition()

    def load(self, saved: dict[str, Any]) -> None:
        for session_id, record in saved.items():
            if (
                not isinstance(session_id, str)
                or not isinstance(record, dict)
                or not isinstance(record.get("epoch"), str)
                or not record["epoch"]
                or type(record.get("thread_id")) is not int
                or record["thread_id"] <= 0
                or not isinstance(record.get("attached_at"), (float, int))
                or not math.isfinite(record["attached_at"])
            ):
                raise ValueError("Invalid agent session continuation")
        self.saved = saved

    def export(self) -> dict[str, Any]:
        return {
            session.session_id: {
                "epoch": session.session_epoch,
                "thread_id": session.thread_id,
                "attached_at": session.attached_at.timestamp(),
            }
            for session in self.bridge.sessions.by_session.values()
            if session.thread_id is not None
        }

    def reconnected(self, session: AgentSession, previous: AgentSession | None) -> None:
        if previous is not None and (previous.session_epoch, previous.thread_id) == (session.session_epoch, session.thread_id):
            session.attached_at = previous.attached_at
            return
        record = self.saved.get(session.session_id)
        if record is not None and (record["epoch"], record["thread_id"]) == (session.session_epoch, session.thread_id):
            session.attached_at = datetime.fromtimestamp(record["attached_at"], UTC)

    async def notify(self) -> None:
        async with self.changed:
            self.changed.notify_all()

    async def wait_for_thread(self, thread_id: int) -> AgentSession | None:
        session = self.bridge.sessions.get_by_thread(thread_id)
        handover = getattr(self.bridge.bot, "handover", None)
        if handover is None or asyncio.current_task() not in handover.replay:
            return session
        if not any(record["thread_id"] == thread_id for record in self.saved.values()):
            return session

        def ready() -> bool:
            current = self.bridge.sessions.get_by_thread(thread_id)
            return current is not None and current.acknowledged and not current.websocket.closed

        async with self.changed:
            try:
                await asyncio.wait_for(self.changed.wait_for(ready), self.grace_seconds)
            except TimeoutError:
                return self.bridge.sessions.get_by_thread(thread_id)
        return self.bridge.sessions.get_by_thread(thread_id)
