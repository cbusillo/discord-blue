from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
import math
from typing import Any, TYPE_CHECKING

from discord_blue.doodads.agent_session.protocol import RemoteApprovalRequest, RemoteRequestUserInput
from discord_blue.doodads.agent_session.sessions import AgentSession

if TYPE_CHECKING:
    from discord_blue.doodads.agent_session.bridge import AgentSessionBridge

REPLAY_RECONNECT_WAIT_SECONDS = 10.0


class SessionContinuation:
    def __init__(self, bridge: AgentSessionBridge, grace_seconds: float) -> None:
        self.bridge = bridge
        self.grace_seconds = min(grace_seconds, REPLAY_RECONNECT_WAIT_SECONDS)
        self.saved: dict[str, Any] = {}
        self.changed = asyncio.Condition()
        self.incoming: dict[asyncio.Task[Any], int] = {}
        self.snapshot: dict[str, Any] | None = None
        self.fallbacks = 0

    @contextmanager
    def section(self) -> Iterator[None]:
        task = asyncio.current_task()
        assert task is not None
        self.incoming[task] = self.incoming.get(task, 0) + 1
        try:
            yield
        finally:
            depth = self.incoming[task] - 1
            if depth:
                self.incoming[task] = depth
            else:
                del self.incoming[task]
                self.notify_soon()

    def fallback(self) -> None:
        if asyncio.current_task() in self.bridge.bot.handover.replay:
            self.fallbacks += 1

    def retire(self, session_id: str, kind: str | None = None, request_id: str | None = None) -> None:
        record = self.saved.get(session_id)
        if record is None:
            return
        if kind is None:
            self.saved.pop(session_id, None)
        elif request_id is None:
            record[kind].clear()
        else:
            record[kind].pop(request_id, None)

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
            datetime.fromtimestamp(record["attached_at"], UTC)
            for kind in ("approvals", "inputs"):
                prompts = record.get(kind)
                if not isinstance(prompts, dict) or any(
                    not isinstance(request_id, str) or type(message_id) is not int or message_id <= 0
                    for request_id, message_id in prompts.items()
                ):
                    raise ValueError("Invalid prompt routing IDs")
        self.saved = saved

    def export(self) -> dict[str, Any]:
        saved = {
            session_id: deepcopy(record)
            for session_id, record in self.saved.items()
            if (stored := self.bridge.store.records.get(session_id)) is not None
            and stored.status not in {"closed", "closing"}
            and stored.thread_id == record["thread_id"]
        }
        for session in self.bridge.sessions.by_session.values():
            if session.ended:
                saved.pop(session.session_id, None)
                continue
            if session.thread_id is None:
                continue
            previous = saved.get(session.session_id, {})
            matching = (previous.get("epoch"), previous.get("thread_id")) == (session.session_epoch, session.thread_id)
            saved[session.session_id] = {
                "epoch": session.session_epoch,
                "thread_id": session.thread_id,
                "attached_at": session.attached_at.timestamp(),
                "approvals": {
                    **(previous.get("approvals", {}) if matching else {}),
                    **{
                        key: item.message_id
                        for key, item in session.pending_approvals.items()
                        if not item.retired and item.decision is None
                    },
                },
                "inputs": {
                    **(previous.get("inputs", {}) if matching else {}),
                    **{
                        key: item.message_id
                        for key, item in session.pending_user_inputs.items()
                        if not item.retired and not item.submitted
                    },
                },
            }
        return saved

    async def restore_prompt(self, session: AgentSession, message_type: object, payload: dict[str, object]) -> bool:
        if payload.get("session_id") != session.session_id or payload.get("session_epoch") != session.session_epoch:
            return False
        record = self.saved.get(session.session_id)
        if message_type == "approval_request":
            approval = RemoteApprovalRequest.from_payload(payload)
            current = session.pending_approvals.get(approval.approval_id)
            if current is not None and not current.retired:
                self.retire(session.session_id, "approvals", approval.approval_id)
                return True
            if record is not None and (message_id := record["approvals"].get(approval.approval_id)) is not None:
                await self.bridge.interactions.handle_approval_request(approval, message_id=message_id)
                self.retire(session.session_id, "approvals", approval.approval_id)
                return True
        elif message_type == "request_user_input":
            request = RemoteRequestUserInput.from_payload(payload)
            current_input = session.pending_user_inputs.get(request.call_id)
            if current_input is not None and not current_input.retired and current_input.turn_id == request.turn_id:
                self.retire(session.session_id, "inputs", request.call_id)
                return True
            if record is not None and (message_id := record["inputs"].get(request.call_id)) is not None:
                await self.bridge.interactions.handle_request_user_input(request, message_id=message_id)
                self.retire(session.session_id, "inputs", request.call_id)
                return True
        return False

    def reconnected(self, session: AgentSession, previous: AgentSession | None) -> None:
        record = self.saved.get(session.session_id)
        if record is not None and (record["epoch"], record["thread_id"]) != (session.session_epoch, session.thread_id):
            self.retire(session.session_id)
            record = None
        if previous is not None and (previous.session_epoch, previous.thread_id) == (session.session_epoch, session.thread_id):
            session.attached_at = previous.attached_at
            session.pending_commands = previous.pending_commands
            session.pending_approvals = previous.pending_approvals
            session.pending_user_inputs = previous.pending_user_inputs
            session.active_command_id = previous.active_command_id
            session.status_revision = previous.status_revision
            session.display_state = previous.display_state
            session.last_status_message = previous.last_status_message
        elif record is not None:
            session.attached_at = datetime.fromtimestamp(record["attached_at"], UTC)

    async def notify(self) -> None:
        async with self.changed:
            self.changed.notify_all()

    def notify_soon(self) -> None:
        task = asyncio.create_task(self.notify())
        self.bridge.threads.background.add(task)
        task.add_done_callback(self.bridge.threads.request_done)

    def has_thread(self, thread_id: int) -> bool:
        return any(record["thread_id"] == thread_id for record in self.saved.values()) or any(
            record.thread_id == thread_id and record.status not in {"closed", "closing"}
            for record in self.bridge.store.records.values()
        )

    async def wait_for_thread(self, thread_id: int) -> AgentSession | None:
        session = self.bridge.sessions.get_by_thread(thread_id)
        if not self.has_thread(thread_id) and (session is None or session.acknowledged):
            return session

        def ready() -> bool:
            current = self.bridge.sessions.get_by_thread(thread_id)
            return current is not None and current.acknowledged and not current.websocket.closed

        async with self.changed:
            try:
                await asyncio.wait_for(self.changed.wait_for(ready), self.grace_seconds)
            except TimeoutError:
                self.fallback()
        return self.bridge.sessions.get_by_thread(thread_id)

    def has_prompt(self, thread_id: int, message_id: int, *, kind: str = "approvals") -> bool:
        return any(message_id in record[kind].values() for record in self.saved.values() if record["thread_id"] == thread_id)

    async def wait_for_prompt(self, thread_id: int, message_id: int) -> None:
        async with self.changed:
            try:
                await asyncio.wait_for(
                    self.changed.wait_for(
                        lambda: not any(self.has_prompt(thread_id, message_id, kind=kind) for kind in ("approvals", "inputs"))
                    ),
                    self.grace_seconds,
                )
            except TimeoutError:
                self.fallback()
