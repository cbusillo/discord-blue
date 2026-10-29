from __future__ import annotations

import asyncio
from dataclasses import asdict, fields
from datetime import UTC, datetime
import math
from typing import Any, TYPE_CHECKING

import discord

from discord_blue.doodads.agent_session.interactions import ApprovalButton, ApprovalView, RequestUserInputView
from discord_blue.doodads.agent_session.chunks import DISCORD_MESSAGE_LIMIT
from discord_blue.doodads.agent_session.protocol import RemoteApprovalRequest, RemoteRequestUserInput
from discord_blue.doodads.agent_session.sessions import (
    AgentSession,
    PendingRemoteApproval,
    PendingRemoteCommand,
    PendingRemoteUserInput,
)

if TYPE_CHECKING:
    from discord_blue.doodads.agent_session.bridge import AgentSessionBridge


class SessionContinuation:
    def __init__(self, bridge: AgentSessionBridge, grace_seconds: float) -> None:
        self.bridge = bridge
        self.grace_seconds = grace_seconds
        self.saved: dict[str, Any] = {}
        self.changed = asyncio.Condition()
        self.prompts: dict[tuple[str, str], object] = {}
        self.restored_sessions: set[str] = set()

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
        for session_id, record in saved.items():
            for kind in ("approvals", "inputs"):
                for prompt in record.get(kind, {}).values():
                    view = self.restore_view(session_id, kind, prompt)
                    self.bridge.bot._connection.store_view(view, prompt["message_id"])

    def export(self) -> dict[str, Any]:
        saved: dict[str, dict[str, Any]] = {
            session.session_id: {
                "epoch": session.session_epoch,
                "thread_id": session.thread_id,
                "attached_at": session.attached_at.timestamp(),
                "approvals": {},
                "inputs": {},
                "commands": {
                    command_id: {
                        field.name: getattr(command, field.name) for field in fields(command) if field.name != "input_prompt"
                    }
                    for command_id, command in session.pending_commands.items()
                },
                "active_command_id": session.active_command_id,
                "status_revision": session.status_revision,
                "display_state": session.display_state,
                "last_status_message": session.last_status_message,
            }
            for session in self.bridge.sessions.by_session.values()
            if session.thread_id is not None
        }
        for session in self.bridge.sessions.by_session.values():
            if session.session_id not in saved:
                continue
            record = saved[session.session_id]
            for approval_id, pending in session.pending_approvals.items():
                if pending.view is None or pending.retired:
                    continue
                button = next((item for item in pending.view.walk_children() if isinstance(item, ApprovalButton)), None)
                if button is not None:
                    record["approvals"][approval_id] = {
                        "request": asdict(button.approval),
                        "message_id": pending.message_id,
                        "controls": self.control_ids(pending.view),
                        "decision": pending.decision,
                        "decided_by": pending.decided_by,
                    }
            for call_id, pending_input in session.pending_user_inputs.items():
                view = self.bridge.bot._connection._view_store._synced_message_views.get(pending_input.message_id)
                if isinstance(view, RequestUserInputView) and not pending_input.retired:
                    record["inputs"][call_id] = {
                        "request": asdict(view.request),
                        "message_id": pending_input.message_id,
                        "controls": self.control_ids(view),
                        "answers": dict(view.answers),
                        "submitted": pending_input.submitted,
                    }
        return saved

    def restore_view(self, session_id: str, kind: str, saved: dict[str, Any]) -> discord.ui.LayoutView:
        if kind == "approvals":
            approval = RemoteApprovalRequest.from_payload(saved["request"])
            content = self.bridge.interactions.format_approval_request(approval)[:DISCORD_MESSAGE_LIMIT]
            view: discord.ui.LayoutView = ApprovalView(self.bridge.interactions, approval, content)
        else:
            request = RemoteRequestUserInput.from_payload(saved["request"])
            restored = RequestUserInputView(self.bridge.interactions, session_id, request)
            restored.answers.update(saved["answers"])
            restored.message_id = saved["message_id"]
            for question in request.questions:
                restored.question_texts[question.id].content = restored.question_text(question)
            view = restored
        self.restore_ids(view, saved["controls"])
        return view

    @staticmethod
    def control_key(item: discord.ui.Item[Any]) -> str:
        if isinstance(item, ApprovalButton):
            return "approval:" + item.decision
        question = getattr(item, "question", None)
        return type(item).__name__ + (":" + question.id if question is not None else "")

    @classmethod
    def control_ids(cls, view: discord.ui.LayoutView) -> dict[str, str]:
        return {
            cls.control_key(item): item.custom_id
            for item in view.walk_children()
            if isinstance(item, (discord.ui.Button, discord.ui.Select)) and item.custom_id is not None
        }

    @classmethod
    def restore_ids(cls, view: discord.ui.LayoutView, controls: dict[str, str]) -> None:
        if cls.control_ids(view).keys() != controls.keys():
            raise ValueError("Pending control shape changed during handover")
        for item in view.walk_children():
            if isinstance(item, (discord.ui.Button, discord.ui.Select)) and item.custom_id is not None:
                item.custom_id = controls[cls.control_key(item)]

    async def restore_prompt(self, session: AgentSession, message_type: object, payload: dict[str, object]) -> bool:
        record = self.saved.get(session.session_id)
        if record is None or record["epoch"] != session.session_epoch or record["thread_id"] != session.thread_id:
            return False
        if message_type == "approval_request":
            approval = RemoteApprovalRequest.from_payload(payload)
            request: RemoteApprovalRequest | RemoteRequestUserInput = approval
            saved = record.get("approvals", {}).get(approval.approval_id)
            kind = "approvals"
        elif message_type == "request_user_input":
            request = RemoteRequestUserInput.from_payload(payload)
            saved = record.get("inputs", {}).get(request.call_id)
            kind = "inputs"
        else:
            return False
        if saved is None:
            return False
        request_type = type(request)
        if not saved["request"].keys() <= {field.name for field in fields(request_type)}:
            raise ValueError("Pending request schema cannot be restored")
        if request_type.from_payload(saved["request"]) != request:
            return False
        channel = self.bridge.thread_channel(record["thread_id"])
        if not isinstance(channel, discord.Thread):
            return False
        try:
            message = await channel.fetch_message(saved["message_id"])
        except discord.NotFound:
            return False
        if message.author.id != self.bridge.bot_user_id():
            raise ValueError("Pending prompt no longer belongs to the bot")
        view = self.restore_view(session.session_id, kind, saved)
        if isinstance(request, RemoteApprovalRequest):
            session.pending_approvals[request.approval_id] = PendingRemoteApproval(
                thread_id=record["thread_id"],
                message_id=message.id,
                view=view,
                decision=saved["decision"],
                decided_by=saved["decided_by"],
            )
            request_id = request.approval_id
        else:
            session.pending_user_inputs[request.call_id] = PendingRemoteUserInput(
                record["thread_id"], message.id, request.turn_id, request.call_id, submitted=saved["submitted"]
            )
            request_id = request.call_id
        await message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
        self.prompts[(session.session_id, kind + ":" + request_id)] = request
        return True

    def reconnected(self, session: AgentSession, previous: AgentSession | None) -> None:
        if previous is not None and (previous.session_epoch, previous.thread_id) == (session.session_epoch, session.thread_id):
            session.attached_at = previous.attached_at
            return
        record = self.saved.get(session.session_id)
        if record is not None and (record["epoch"], record["thread_id"]) == (session.session_epoch, session.thread_id):
            session.attached_at = datetime.fromtimestamp(record["attached_at"], UTC)
            if session.session_id not in self.restored_sessions:
                session.pending_commands = {
                    command_id: PendingRemoteCommand(**command) for command_id, command in record.get("commands", {}).items()
                }
                session.active_command_id = record.get("active_command_id")
                session.status_revision = record.get("status_revision", 0)
                session.display_state = record.get("display_state", "unknown")
                session.last_status_message = record.get("last_status_message")
                self.restored_sessions.add(session.session_id)

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
            await asyncio.wait_for(self.changed.wait_for(ready), self.grace_seconds)
        return self.bridge.sessions.get_by_thread(thread_id)

    async def wait_for_prompt(self, thread_id: int, message_id: int) -> None:
        target = next(
            (
                (session_id, record["epoch"], kind + ":" + request_id)
                for session_id, record in self.saved.items()
                if record["thread_id"] == thread_id
                for kind in ("approvals", "inputs")
                for request_id, saved in record.get(kind, {}).items()
                if saved["message_id"] == message_id
            ),
            None,
        )
        if target is None:
            return
        session_id, epoch, request_key = target

        def ready() -> bool:
            session = self.bridge.sessions.get(session_id)
            return session is not None and (session.session_epoch != epoch or (session_id, request_key) in self.prompts)

        async with self.changed:
            await asyncio.wait_for(self.changed.wait_for(ready), self.grace_seconds)
