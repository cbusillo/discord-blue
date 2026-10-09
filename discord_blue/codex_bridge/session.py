"""One Codex thread mirrored as one Discord Blue agent session.

Events from the app-server daemon are queued and delivered over the session's
WebSocket; Discord controls are translated into app-server calls. The bridge
answers a Codex request only after an explicit Discord decision; the first
response wins in stock, so the local TUI can still answer instead.

The Discord session lives as long as the Codex thread stays loaded, but the bridge
subscribes to the thread only while a turn runs, a prompt is pending, or Discord
acts. Unsubscribed, it still sees the broadcast status changes that say a turn
started, and stock can unload the thread once its TUI closes.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any, Protocol

from discord_blue.agent_client import AgentSessionClient, Rejected
from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.codex_bridge.approvals import FILE_APPROVAL, PERMISSIONS_APPROVAL, content_snapshot
from discord_blue.codex_bridge.rpc import RequestId, RpcError, TransportError
from discord_blue.doodads.agent_session.protocol import command_text_displayable
from discord_blue.session_titles import SessionLabel

Json = dict[str, Any]
logger = logging.getLogger(__name__)

CAPABILITIES = ["new_session", "approval_decision", "pause_current_turn", "reply", "request_user_input_response", "status_request"]
COMMAND_APPROVAL = "item/commandExecution/requestApproval"
USER_INPUT = "item/tool/requestUserInput"
LOCAL_DECISIONS = {"item/fileChange/requestApproval", "item/permissions/requestApproval", "mcpServer/elicitation/request"}
# Command-approval fields Discord may leave unshown: routing, display hints, and amendments that a
# plain `accept` never applies. Any other field set (additionalPermissions, networkApprovalContext,
# or one added later) can widen what `accept` grants, so that request stays in the TUI.
DISCORD_APPROVABLE_FIELDS = frozenset(
    {
        "kind",
        "threadId",
        "turnId",
        "itemId",
        "startedAtMs",
        "approvalId",
        "environmentId",
        "reason",
        "command",
        "cwd",
        "commandActions",
        "proposedExecpolicyAmendment",
        "proposedNetworkPolicyAmendments",
        "availableDecisions",
    }
)
TEXT_LIMIT = 32_000
REPLY_LIMIT = 16_000
SEEN_LIMIT = 256
TURN_DONE = "Turn complete. Replies here will start the next turn."
LOST_CODEX = "Lost the Codex connection, so delivery is uncertain. Check the Codex TUI before retrying."


class Rpc(Protocol):
    async def request(self, method: str, params: Json | None = None) -> Json: ...

    async def respond(self, request_id: RequestId, result: Json | None = None) -> None: ...


__all__ = ["Rejected", "Rpc", "ThreadSession", "latest_turn"]


def command_argv(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return [command]


def discord_command(params: Json) -> str | None:
    """The command of a plain command approval that Discord can show verbatim and whole, or None to keep it in the TUI."""
    command = params.get("command")
    if params.get("kind", "command") != "command" or not isinstance(command, str):
        return None
    if any(value is not None for key, value in params.items() if key not in DISCORD_APPROVABLE_FIELDS):
        return None
    return command if command_text_displayable(command) else None


def is_answer(item: Json) -> bool:
    return item.get("type") == "agentMessage" and isinstance(item.get("text"), str)


def final_answer(parts: list[tuple[object, str]]) -> str | None:
    finals = [text for phase, text in parts if phase == "final_answer"] or [text for _phase, text in parts[-1:]]
    text = "\n\n".join(finals)
    return text[:TEXT_LIMIT] + "\n[Truncated; see the Codex TUI.]" if len(text) > TEXT_LIMIT else text or None


def latest_turn(page: Json) -> Json:
    return (page.get("data") or [{}])[0]


def user_text(item: Json) -> str:
    return "\n".join(str(part.get("text") or "") for part in item.get("content") or [] if part.get("type") == "text")


class ThreadSession(AgentSessionClient):
    capabilities = CAPABILITIES
    command_errors = (RpcError, TransportError)

    def __init__(
        self,
        config: BridgeConfig,
        rpc: Rpc,
        thread: Json,
        latest: Json,
        *,
        start_session: Callable[[str], Awaitable[None]] | None = None,
        owned: bool = False,
    ) -> None:
        super().__init__(config, thread["id"])
        self.config: BridgeConfig = config
        self.rpc = rpc
        self.start_session = start_session
        self.owned = owned
        self.file_items: dict[tuple[str, str], Json] = {}
        self.approval_responses: dict[RequestId, Json] = {}
        self.thread_id: str = thread["id"]
        self.cwd = str(thread.get("cwd") or "")
        self.branch = (thread.get("gitInfo") or {}).get("branch")
        self.label = SessionLabel(name=thread.get("name"), prompt=thread.get("preview"))
        self.active_turn_id: str | None = None
        self.subscribed = False
        self.membership = asyncio.Lock()
        # Turns and items already mirrored, so catching up after a join never repeats them.
        self.reported_turns: deque[str] = deque(maxlen=SEEN_LIMIT)
        self.seen_items: deque[str] = deque(maxlen=SEEN_LIMIT)
        self.backfill: str | None = None
        if latest.get("id") and latest.get("status") != "inProgress":
            self.reported_turns.append(str(latest["id"]))
            self.seen_items.extend(str(i["id"]) for i in latest.get("items") or [] if i.get("id"))
            self.backfill = final_answer([(i.get("phase"), i["text"]) for i in latest.get("items") or [] if is_answer(i)])
        self.prompts: dict[RequestId, Json] = {}
        self.approvals: dict[str, RequestId] = {}
        self.inputs: dict[str, RequestId] = {}
        self.answered: set[RequestId] = set()
        self.echo_ids: deque[str] = deque(maxlen=64)
        self.answers: dict[str, list[tuple[object, str]]] = {}

    # Codex -> Discord

    def on_turn_started(self, turn_id: str) -> None:
        if turn_id == self.active_turn_id or turn_id in self.reported_turns:
            return
        self.active_turn_id = turn_id
        self.publish("status_changed", message="Turn started")

    def on_file_item(self, turn_id: str, item: Json) -> None:
        if item.get("type") != "fileChange" or not item.get("id"):
            return
        key = (turn_id, str(item["id"]))
        previous = self.file_items.get(key)
        if previous is not None and previous != item:
            # Never answer a changed patch using a decision on an older display.
            for request_id, prompt in list(self.prompts.items()):
                if prompt.get("approval_kind") == "file_change" and (prompt["turn_id"], prompt["call_id"]) == key:
                    self.on_resolved(request_id)
                    self.publish("notice", message="The patch changed; review this request in the Codex TUI.")
        if len(self.file_items) >= SEEN_LIMIT and key not in self.file_items:
            self.file_items.pop(next(iter(self.file_items)))
        self.file_items[key] = deepcopy(item)

    def on_item_completed(self, turn_id: str, item: Json) -> None:
        if item_id := item.get("id"):
            if str(item_id) in self.seen_items:
                return
            self.seen_items.append(str(item_id))
        if item.get("type") == "userMessage":
            if item.get("clientId") in self.echo_ids:
                return
            if text := user_text(item).strip():
                self.publish("user_message", message=text)
                self.retitle(prompt=text)
        elif is_answer(item) and len(self.answers) < 64:
            self.answers.setdefault(turn_id, []).append((item.get("phase"), item["text"]))

    def rename(self, name: object) -> None:
        """thread/name/updated: a name wins over prompts; clearing it falls back to the first substantial prompt."""
        if isinstance(name, str) and name.strip():
            self.retitle(name=name)
        else:
            self.retitle(clear_name=True)

    def retitle(self, *, name: str | None = None, prompt: str | None = None, clear_name: bool = False) -> None:
        if (title := self.label.update(name=name, prompt=prompt, clear_name=clear_name)) is not None:
            self.publish("title_changed", title=title)

    def on_turn_completed(self, turn: Json) -> None:
        turn_id = str(turn.get("id"))
        if turn_id in self.reported_turns:
            return
        # The completed turn carries its summary items; they fill in anything missed before a join.
        for item in turn.get("items") or []:
            self.on_file_item(turn_id, item)
            self.on_item_completed(turn_id, item)
        self.reported_turns.append(turn_id)
        parts = self.answers.pop(turn_id, [])
        if self.active_turn_id == turn_id:
            self.active_turn_id = None
        # A finished turn's requests can no longer be answered.
        for request_id, prompt in list(self.prompts.items()):
            if prompt["turn_id"] == turn_id:
                self.on_resolved(request_id)
        status = turn.get("status")
        if status == "completed":
            self.publish("turn_complete", message=TURN_DONE, assistant_message=final_answer(parts))
        elif status == "interrupted":
            self.publish("status_changed", message="Turn aborted")
        else:
            self.publish("error", message=str((turn.get("error") or {}).get("message") or "Turn failed"))

    def on_status(self, status: Json) -> None:
        if status.get("type") == "systemError":
            self.publish("error", message="Codex reported a system error; check the Codex TUI.")

    def catch_up(self, turn: Json) -> None:
        """Mirror the latest turn's progress from before this connection joined."""
        turn_id = str(turn.get("id") or "")
        if not turn_id:
            return
        if turn.get("status") == "inProgress":
            self.on_turn_started(turn_id)
            for item in turn.get("items") or []:
                self.on_file_item(turn_id, item)
                self.on_item_completed(turn_id, item)
        else:
            self.on_turn_completed(turn)

    async def subscribe(self) -> None:
        """Join the thread so its turn events and pending requests reach this connection."""
        async with self.membership:
            if self.subscribed:
                return
            thread = (await self.rpc.request("thread/read", {"threadId": self.thread_id}))["thread"]
            # thread/resume would load a closed thread from disk; never do that.
            if (thread.get("status") or {}).get("type") == "notLoaded":
                raise Rejected("This Codex thread has closed; reopen it in the Codex TUI.")
            # No config overrides: they can restart an idle thread cold. Stock replays pending requests.
            await self.rpc.request("thread/resume", {"threadId": self.thread_id, "excludeTurns": True})
            self.subscribed = True
            page = await self.rpc.request("thread/turns/list", {"threadId": self.thread_id, "limit": 1, "itemsView": "summary"})
        self.catch_up(latest_turn(page))

    async def release(self) -> None:
        """Leave the thread once nothing needs this connection, so it can unload when its TUI closes."""
        async with self.membership:
            if self.owned or not self.subscribed or self.active_turn_id is not None or self.prompts:
                return
            self.subscribed = False
            try:
                await self.rpc.request("thread/unsubscribe", {"threadId": self.thread_id})
            except RpcError as exc:
                logger.warning("Could not unsubscribe from Codex thread %s: %s", self.thread_id, exc)

    def on_request(self, request_id: RequestId, method: str, params: Json) -> None:
        if request_id in self.prompts:
            return  # Stock replays pending requests when this connection rejoins.
        turn_id = str(params.get("turnId") or "")
        # A server that did not list command_text would show the re-quoted argv, not Codex's command.
        shows_command_text = self.server_features is None or "command_text" in self.server_features
        if method == COMMAND_APPROVAL and shows_command_text and (command := discord_command(params)) is not None:
            approval_id = str(params.get("approvalId") or params.get("itemId") or request_id)
            self.approvals[approval_id] = request_id
            self.prompts[request_id] = self.event(
                "approval_request",
                approval_id=approval_id,
                call_id=str(params.get("itemId") or ""),
                turn_id=turn_id,
                # Discord shows command_text verbatim; servers that predate it render this argv instead.
                command=command_argv(command),
                command_text=command,
                cwd=str(params.get("cwd") or self.cwd),
                reason=params.get("reason"),
            )
        elif method in {FILE_APPROVAL, PERMISSIONS_APPROVAL}:
            item = self.file_items.get((turn_id, str(params.get("itemId"))))
            snapshot = content_snapshot(method, params, item, self.cwd)
            if snapshot is None or self.server_features is None or "approval_content" not in self.server_features:
                self.publish("status_changed", message="Waiting on a decision in the Codex TUI")
                return
            label, text, response = snapshot
            # Every prompt has its own opaque ID, even when stock reuses an item ID.
            approval_id = uuid.uuid4().hex
            self.approvals[approval_id] = request_id
            self.approval_responses[request_id] = response
            self.prompts[request_id] = self.event(
                "approval_request",
                approval_id=approval_id,
                call_id=params["itemId"],
                turn_id=turn_id,
                approval_kind=label,
                content_text=text,
            )
        elif method == USER_INPUT:
            call_id = str(params.get("itemId") or request_id)
            self.inputs[call_id] = request_id
            questions = [
                {"isOther": False, "isSecret": False, **q, "options": q.get("options") or []} for q in params.get("questions") or []
            ]
            self.prompts[request_id] = self.event("request_user_input", call_id=call_id, turn_id=turn_id, questions=questions)
        else:
            # File-change, permissions, elicitation, scope-widening or undisplayable commands and all
            # other requests stay local; never answer them here.
            if method in LOCAL_DECISIONS or method == COMMAND_APPROVAL:
                self.publish("status_changed", message="Waiting on a decision in the Codex TUI")
            return
        self.enqueue(self.prompts[request_id])

    def on_server_features(self) -> None:
        if self.server_features is None or "approval_content" not in self.server_features:
            for request_id, prompt in list(self.prompts.items()):
                if prompt.get("content_text") is not None:
                    self.on_resolved(request_id)
                    self.publish("status_changed", message="Waiting on a decision in the Codex TUI")
        if self.server_features is not None and "command_text" not in self.server_features:
            self.keep_approvals_local()

    def keep_approvals_local(self) -> None:
        """Take pending approvals back from a server that would show the re-quoted argv; the TUI still has them."""
        for request_id, prompt in list(self.prompts.items()):
            if prompt["type"] == "approval_request":
                del self.prompts[request_id]
                self.approvals.pop(str(prompt["approval_id"]), None)
                self.publish("status_changed", message="Waiting on a decision in the Codex TUI")

    def on_resolved(self, request_id: RequestId) -> None:
        prompt = self.prompts.pop(request_id, None)
        if prompt is None:
            return
        self.approval_responses.pop(request_id, None)
        self.approvals.pop(str(prompt.get("approval_id")), None)
        self.inputs.pop(str(prompt.get("call_id")), None)
        if request_id in self.answered:
            self.answered.discard(request_id)
        elif prompt["type"] == "approval_request":
            self.publish("approval_resolved", approval_id=prompt["approval_id"])
        else:
            self.publish("request_user_input_resolved", call_id=prompt["call_id"], turn_id=prompt["turn_id"])

    # Discord -> Codex

    def failure_reason(self, exc: Exception) -> str:
        if isinstance(exc, RpcError):
            return "Codex rejected the command; check the Codex TUI."
        return LOST_CODEX

    async def run_command(self, message: Json) -> None:
        kind = message.get("kind")
        if kind == "reply":
            text = message.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > REPLY_LIMIT:
                raise Rejected(f"Replies must contain 1 to {REPLY_LIMIT} characters.")
            client_id = f"discord-blue-{uuid.uuid4().hex}"
            self.echo_ids.append(client_id)
            await self.subscribe()
            # Stock turn/start steers the active turn or starts a new one.
            params = {"threadId": self.thread_id, "input": [{"type": "text", "text": text}], "clientUserMessageId": client_id}
            try:
                await self.rpc.request("turn/start", params)
            except RpcError:
                await self.release()
                raise
        elif kind == "new_session":
            if self.start_session is None or not self.cwd:
                raise Rejected("This bridge cannot start a session in this folder.")
            await self.start_session(self.cwd)
        elif kind == "pause_current_turn":
            if self.active_turn_id is None:
                raise Rejected("There is no running turn to pause.")
            await self.rpc.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.active_turn_id})
        elif kind == "status_request":
            self.enqueue(self.status_snapshot())
        elif kind == "request_user_input_response":
            call_id, response = message.get("call_id"), message.get("response")
            request_id = self.inputs.get(str(call_id))
            prompt = self.prompts.get(request_id) if request_id is not None else None
            if request_id is None or prompt is None or prompt["turn_id"] != message.get("turn_id"):
                raise Rejected("This prompt is no longer pending.")
            raw = response.get("answers") if isinstance(response, dict) else None
            answers = {
                str(qid): {"answers": [str(a) for a in value.get("answers") or []]}
                for qid, value in (raw or {}).items()
                if isinstance(value, dict)
            }
            self.inputs.pop(str(call_id))
            self.answered.add(request_id)
            await self.rpc.respond(request_id, {"answers": answers})
        else:
            raise Rejected("The Codex bridge does not support this action; use the Codex TUI.")

    async def approval_decision(self, message: Json) -> Json:
        approval_id = str(message.get("approval_id") or "")
        decision = {"approved": "accept", "denied": "decline"}.get(str(message.get("decision")))
        reject = self.event("approval_decision_reject", approval_id=approval_id)
        if not self.is_current(message):
            return {**reject, "reason": "Stale session; the decision was not sent."}
        request_id = self.approvals.get(approval_id)
        if request_id is None or decision is None:
            return {**reject, "reason": "This approval is no longer pending."}
        self.approvals.pop(approval_id)
        self.answered.add(request_id)
        try:
            response = self.approval_responses.pop(request_id, {"decision": decision})
            if decision == "decline":
                response = {"permissions": {}, "scope": "turn"} if "permissions" in response else {"decision": "decline"}
            await self.rpc.respond(request_id, response)
        except TransportError:
            return {**reject, "reason": LOST_CODEX}
        return self.event("approval_decision_ack", approval_id=approval_id)

    # Discord Blue connection

    def hello(self, *, first: bool) -> Json:
        hello = self.event(
            "hello", host_label=self.config.host_label, cwd=self.cwd, pid=0, capabilities=self.capabilities, harness="codex"
        )
        optional = {"branch": self.branch, "title": self.label.current, "assistant_message": self.backfill if first else None}
        return {**hello, **{key: value for key, value in optional.items() if value}}
