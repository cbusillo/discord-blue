from __future__ import annotations

import asyncio
from dataclasses import asdict, fields
from datetime import UTC, datetime
import math
from typing import Any, TYPE_CHECKING, Literal, cast

import discord

from discord_blue.doodads.agent_session.interactions import ApprovalButton, ApprovalView, RequestUserInputView
from discord_blue.doodads.agent_session.chunks import DISCORD_MESSAGE_LIMIT
from discord_blue.doodads.agent_session.protocol import RemoteApprovalDecision, RemoteApprovalRequest, RemoteRequestUserInput
from discord_blue.doodads.agent_session.sessions import (
    AgentSession,
    PendingRemoteApproval,
    PendingRemoteCommand,
    PendingRemoteUserInput,
)

if TYPE_CHECKING:
    from discord_blue.doodads.agent_session.bridge import AgentSessionBridge

REPLAY_RECONNECT_WAIT_SECONDS = 10.0


class SessionContinuation:
    def __init__(self, bridge: AgentSessionBridge, grace_seconds: float) -> None:
        self.bridge = bridge
        self.grace_seconds = min(grace_seconds, REPLAY_RECONNECT_WAIT_SECONDS)
        self.saved: dict[str, Any] = {}
        self.changed = asyncio.Condition()
        self.restored_sessions: set[str] = set()
        self.incoming: set[asyncio.Task[Any]] = set()
        self.snapshot: dict[str, Any] | None = None

    def load(self, saved: dict[str, Any]) -> None:
        views: list[tuple[discord.ui.LayoutView, int]] = []
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
            commands = record.get("commands", {})
            if not isinstance(commands, dict):
                raise ValueError("Invalid command continuation")
            for command in commands.values():
                self.restore_command(command)
            for kind in ("approvals", "inputs"):
                prompts = record.get(kind, {})
                if not isinstance(prompts, dict):
                    raise ValueError("Invalid prompt continuation")
                for request_id, prompt in prompts.items():
                    request = prompt["request"]
                    if (
                        request["session_id"] != session_id
                        or request["session_epoch"] != record["epoch"]
                        or request["approval_id" if kind == "approvals" else "call_id"] != request_id
                        or type(prompt["message_id"]) is not int
                        or prompt["message_id"] <= 0
                    ):
                        raise ValueError("Invalid prompt continuation binding")
                    view = self.restore_view(session_id, kind, prompt)
                    views.append((view, prompt["message_id"]))
        self.saved = saved
        for view, message_id in views:
            self.bridge.bot._connection.store_view(view, message_id)

    def export(self) -> dict[str, Any]:
        saved: dict[str, dict[str, Any]] = {
            session.session_id: {
                "epoch": session.session_epoch,
                "thread_id": session.thread_id,
                "attached_at": session.attached_at.timestamp(),
                "approvals": {},
                "inputs": {},
                "commands": {command_id: self.command_state(command) for command_id, command in session.pending_commands.items()},
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
                    }
        return saved

    @staticmethod
    def command_state(command: PendingRemoteCommand) -> dict[str, Any]:
        saved = {field.name: getattr(command, field.name) for field in fields(command) if field.name != "input_prompt"}
        if command.input_prompt is not None:
            saved["input_prompt"] = {
                field.name: getattr(command.input_prompt, field.name)
                for field in fields(command.input_prompt)
                if field.name != "ui_lock"
            }
        return saved

    @staticmethod
    def restore_command(saved: dict[str, Any]) -> PendingRemoteCommand:
        values = dict(saved)
        values["question_keys"] = tuple(values.get("question_keys", ()))
        if values.get("input_prompt") is not None:
            values["input_prompt"] = PendingRemoteUserInput(**values["input_prompt"])
        return PendingRemoteCommand(**values)

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
            return self.current_prompt_matches(session, request)
        request_type = type(request)
        if not saved["request"].keys() <= {field.name for field in fields(request_type)}:
            raise ValueError("Pending request schema cannot be restored")
        if request_type.from_payload(saved["request"]) != request:
            record[kind].pop(request.approval_id if isinstance(request, RemoteApprovalRequest) else request.call_id)
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
            if saved["decision"] in {"approved", "denied"}:
                await session.websocket.send_json(
                    RemoteApprovalDecision(
                        request.approval_id,
                        session.session_id,
                        session.session_epoch,
                        cast(Literal["approved", "denied"], saved["decision"]),
                    ).to_message()
                )
            request_id = request.approval_id
        else:
            pending_input = PendingRemoteUserInput(record["thread_id"], message.id, request.turn_id, request.call_id)
            session.pending_user_inputs[request.call_id] = pending_input
            for command in session.pending_commands.values():
                if command.input_prompt is not None and command.input_prompt.call_id == request.call_id:
                    command.input_prompt = pending_input
            request_id = request.call_id
        await message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
        record[kind].pop(request_id)
        return True

    def current_prompt_matches(self, session: AgentSession, request: RemoteApprovalRequest | RemoteRequestUserInput) -> bool:
        if isinstance(request, RemoteApprovalRequest):
            pending = session.pending_approvals.get(request.approval_id)
            if pending is None or pending.retired or pending.view is None:
                return False
            return any(isinstance(item, ApprovalButton) and item.approval == request for item in pending.view.walk_children())
        pending_input = session.pending_user_inputs.get(request.call_id)
        if pending_input is None or pending_input.retired:
            return False
        view = self.bridge.bot._connection._view_store._synced_message_views.get(pending_input.message_id)
        return isinstance(view, RequestUserInputView) and view.request == request

    def reconnected(self, session: AgentSession, previous: AgentSession | None) -> None:
        if previous is not None and (previous.session_epoch, previous.thread_id) == (session.session_epoch, session.thread_id):
            session.attached_at = previous.attached_at
            session.pending_commands = previous.pending_commands
            session.pending_approvals = previous.pending_approvals
            session.pending_user_inputs = previous.pending_user_inputs
            session.active_command_id = previous.active_command_id
            session.status_revision = previous.status_revision
            session.display_state = previous.display_state
            session.last_status_message = previous.last_status_message
            return
        record = self.saved.get(session.session_id)
        if record is not None and (record["epoch"], record["thread_id"]) == (session.session_epoch, session.thread_id):
            session.attached_at = datetime.fromtimestamp(record["attached_at"], UTC)
            if session.session_id not in self.restored_sessions:
                session.pending_commands = {
                    command_id: self.restore_command(command) for command_id, command in record.get("commands", {}).items()
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
        if handover is None or not handover.replay:
            return session
        if not any(record["thread_id"] == thread_id for record in self.saved.values()):
            return session

        def ready() -> bool:
            current = self.bridge.sessions.get_by_thread(thread_id)
            return current is not None and current.acknowledged and not current.websocket.closed

        async with self.changed:
            await asyncio.wait_for(self.changed.wait_for(ready), self.grace_seconds)
        return self.bridge.sessions.get_by_thread(thread_id)

    def has_prompt(self, thread_id: int, message_id: int) -> bool:
        return any(
            saved["message_id"] == message_id
            for record in self.saved.values()
            if record["thread_id"] == thread_id
            for saved in record.get("approvals", {}).values()
        )

    async def settle(self) -> bool:
        async with self.changed:
            try:
                await asyncio.wait_for(self.changed.wait_for(lambda: not self.incoming), self.grace_seconds)
            except TimeoutError:
                return False
        self.snapshot = self.export()
        return True

    async def wait_for_prompt(self, thread_id: int, message_id: int) -> None:
        target = next(
            (
                (session_id, record["epoch"])
                for session_id, record in self.saved.items()
                if record["thread_id"] == thread_id
                for kind in ("approvals", "inputs")
                for saved in record.get(kind, {}).values()
                if saved["message_id"] == message_id
            ),
            None,
        )
        if target is None:
            return
        session_id, epoch = target

        def ready() -> bool:
            session = self.bridge.sessions.get(session_id)
            return session is not None and (session.session_epoch != epoch or not self.has_prompt(thread_id, message_id))

        async with self.changed:
            await asyncio.wait_for(self.changed.wait_for(ready), self.grace_seconds)
