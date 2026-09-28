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
import json
import logging
import shlex
import uuid
from collections import OrderedDict, deque
from typing import Any, Protocol

import aiohttp

from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.codex_bridge.rpc import RequestId, RpcError, TransportError
from discord_blue.doodads.agent_session.protocol import APPROVAL_COMMAND_DISPLAY_LIMIT

Json = dict[str, Any]
logger = logging.getLogger(__name__)

CAPABILITIES = ["approval_decision", "pause_current_turn", "reply", "request_user_input_response", "status_request"]
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
PROMPT_EVENTS = {"approval_request", "request_user_input"}
STATUS_EVENTS = {"status_changed", "turn_complete", "error"}
OUTBOX_LIMIT = 256
COMMAND_MEMORY = 1024
TEXT_LIMIT = 32_000
REPLY_LIMIT = 16_000
TITLE_LIMIT = 80
SEEN_LIMIT = 256
TURN_DONE = "Turn complete. Replies here will start the next turn."
LOST_CODEX = "Lost the Codex connection, so delivery is uncertain. Check the Codex TUI before retrying."


class Rpc(Protocol):
    async def request(self, method: str, params: Json | None = None) -> Json: ...

    async def respond(self, request_id: RequestId, result: Json | None = None) -> None: ...


class Rejected(Exception):
    """A control that was not executed, with a reason safe to show in Discord."""


def thread_title(thread: Json) -> str | None:
    text = str(thread.get("name") or thread.get("preview") or "").strip()
    first_line = text.splitlines()[0] if text else ""
    return first_line[: TITLE_LIMIT - 1] + "…" if len(first_line) > TITLE_LIMIT else first_line or None


def command_argv(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return [command]


def discord_command(params: Json) -> list[str] | None:
    """The argv of a plain command approval that Discord can show in full, or None to keep it in the TUI."""
    command = params.get("command")
    if params.get("kind", "command") != "command" or not isinstance(command, str):
        return None
    if any(value is not None for key, value in params.items() if key not in DISCORD_APPROVABLE_FIELDS):
        return None
    argv = command_argv(command)
    # Discord shows the joined argv in a code fence, truncated; a fence inside it would end the block early.
    shown = shlex.join(argv)
    return argv if len(shown) <= APPROVAL_COMMAND_DISPLAY_LIMIT and "```" not in shown else None


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


class ThreadSession:
    def __init__(self, config: BridgeConfig, rpc: Rpc, thread: Json, latest: Json) -> None:
        self.config, self.rpc = config, rpc
        self.thread_id: str = thread["id"]
        self.epoch = uuid.uuid4().hex
        self.cwd = str(thread.get("cwd") or "")
        self.branch = (thread.get("gitInfo") or {}).get("branch")
        self.title = thread_title(thread)
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
        self.outbox: deque[Json] = deque()
        self.wakeup = asyncio.Event()
        self.stopped = asyncio.Event()
        self.websocket: aiohttp.ClientWebSocketResponse | None = None
        self.prompts: dict[RequestId, Json] = {}
        self.approvals: dict[str, RequestId] = {}
        self.inputs: dict[str, RequestId] = {}
        self.answered: set[RequestId] = set()
        self.echo_ids: deque[str] = deque(maxlen=64)
        self.answers: dict[str, list[tuple[object, str]]] = {}
        self.commands: OrderedDict[str, Json] = OrderedDict()
        self.last_status: Json | None = None

    # Codex -> Discord

    def event(self, kind: str, **fields: object) -> Json:
        return {"type": kind, "session_id": self.thread_id, "session_epoch": self.epoch, **fields}

    def publish(self, kind: str, **fields: object) -> None:
        event = self.event(kind, **fields)
        if kind in STATUS_EVENTS:
            self.last_status = event
        self.enqueue(event)

    def enqueue(self, event: Json) -> None:
        if len(self.outbox) >= OUTBOX_LIMIT:
            logger.warning("Dropping the oldest queued event for thread %s", self.thread_id)
            self.outbox.popleft()
        self.outbox.append(event)
        self.wakeup.set()

    def on_turn_started(self, turn_id: str) -> None:
        if turn_id == self.active_turn_id or turn_id in self.reported_turns:
            return
        self.active_turn_id = turn_id
        self.publish("status_changed", message="Turn started")

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
        elif is_answer(item) and len(self.answers) < 64:
            self.answers.setdefault(turn_id, []).append((item.get("phase"), item["text"]))

    def on_turn_completed(self, turn: Json) -> None:
        turn_id = str(turn.get("id"))
        if turn_id in self.reported_turns:
            return
        # The completed turn carries its summary items; they fill in anything missed before a join.
        for item in turn.get("items") or []:
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
            if not self.subscribed or self.active_turn_id is not None or self.prompts:
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
        if method == COMMAND_APPROVAL and (argv := discord_command(params)) is not None:
            approval_id = str(params.get("approvalId") or params.get("itemId") or request_id)
            self.approvals[approval_id] = request_id
            self.prompts[request_id] = self.event(
                "approval_request",
                approval_id=approval_id,
                call_id=str(params.get("itemId") or ""),
                turn_id=turn_id,
                command=argv,
                cwd=str(params.get("cwd") or self.cwd),
                reason=params.get("reason"),
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

    def on_resolved(self, request_id: RequestId) -> None:
        prompt = self.prompts.pop(request_id, None)
        if prompt is None:
            return
        self.approvals.pop(str(prompt.get("approval_id")), None)
        self.inputs.pop(str(prompt.get("call_id")), None)
        if request_id in self.answered:
            self.answered.discard(request_id)
        elif prompt["type"] == "approval_request":
            self.publish("approval_resolved", approval_id=prompt["approval_id"])
        else:
            self.publish("request_user_input_resolved", call_id=prompt["call_id"], turn_id=prompt["turn_id"])

    # Discord -> Codex

    async def handle_control(self, message: Json) -> Json | None:
        if message.get("type") == "approval_decision":
            return await self.approval_decision(message)
        if message.get("type") != "command":
            return None
        command_id = message.get("command_id")
        if message.get("session_id") != self.thread_id or message.get("session_epoch") != self.epoch:
            return self.event("command_reject", command_id=command_id, reason="Stale session; the command was not executed.")
        if not isinstance(command_id, str) or not command_id:
            return self.event("command_reject", command_id=command_id, reason="Invalid command ID.")
        if command_id in self.commands:
            return self.commands[command_id]
        self.commands[command_id] = self.event("command_reject", command_id=command_id, reason="Command already in progress.")
        try:
            await self.run_command(message)
            response = self.event("command_ack", command_id=command_id)
        except Rejected as exc:
            response = self.event("command_reject", command_id=command_id, reason=str(exc))
        except RpcError:
            response = self.event("command_reject", command_id=command_id, reason="Codex rejected the command; check the Codex TUI.")
        except TransportError:
            response = self.event("command_reject", command_id=command_id, reason=LOST_CODEX)
        self.commands[command_id] = response
        while len(self.commands) > COMMAND_MEMORY:
            self.commands.popitem(last=False)
        return response

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
        elif kind == "pause_current_turn":
            if self.active_turn_id is None:
                raise Rejected("There is no running turn to pause.")
            await self.rpc.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.active_turn_id})
        elif kind == "status_request":
            self.enqueue(dict(self.last_status or self.event("status_changed", message="Connected")))
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
        if message.get("session_id") != self.thread_id or message.get("session_epoch") != self.epoch:
            return {**reject, "reason": "Stale session; the decision was not sent."}
        request_id = self.approvals.get(approval_id)
        if request_id is None or decision is None:
            return {**reject, "reason": "This approval is no longer pending."}
        self.approvals.pop(approval_id)
        self.answered.add(request_id)
        try:
            await self.rpc.respond(request_id, {"decision": decision})
        except TransportError:
            return {**reject, "reason": LOST_CODEX}
        return self.event("approval_decision_ack", approval_id=approval_id)

    # Discord Blue connection

    def hello(self, *, first: bool) -> Json:
        hello = self.event("hello", host_label=self.config.host_label, cwd=self.cwd, pid=0, capabilities=CAPABILITIES)
        optional = {"branch": self.branch, "title": self.title, "assistant_message": self.backfill if first else None}
        return {**hello, **{key: value for key, value in optional.items() if value}}

    async def send(self, websocket: aiohttp.ClientWebSocketResponse, message: Json) -> None:
        async with asyncio.timeout(15):
            await websocket.send_json(message)

    async def run(self, http: aiohttp.ClientSession) -> None:
        headers = {"Authorization": f"Bearer {self.config.token}"}
        first = True
        while not self.stopped.is_set():
            try:
                async with http.ws_connect(self.config.server_url, headers=headers, max_msg_size=1024 * 1024) as websocket:
                    self.websocket = websocket
                    await self.send(websocket, self.hello(first=first))
                    async with asyncio.timeout(self.config.hello_timeout_seconds):
                        ack = await websocket.receive_json()
                    if not isinstance(ack, dict) or ack.get("type") != "hello_ack":
                        raise ValueError("Discord Blue did not acknowledge the session")
                    first = False
                    # Prompts retire on disconnect and on every status event, so replay queued history
                    # first and then each still-pending prompt once.
                    self.outbox = deque([*(e for e in self.outbox if e["type"] not in PROMPT_EVENTS), *self.prompts.values()])
                    self.wakeup.set()
                    await self.serve(websocket)
            except aiohttp.WSServerHandshakeError as exc:
                logger.error("Discord Blue refused thread %s (HTTP %s); check server_url and token", self.thread_id, exc.status)
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError, TypeError) as exc:
                logger.warning("Discord Blue connection for thread %s ended: %s", self.thread_id, type(exc).__name__)
            finally:
                self.websocket = None
            try:
                await asyncio.wait_for(self.stopped.wait(), self.config.reconnect_seconds)
            except TimeoutError:
                pass

    async def serve(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        async def deliver() -> None:
            while True:
                await self.wakeup.wait()
                self.wakeup.clear()
                while self.outbox:
                    await self.send(websocket, self.outbox.popleft())

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(self.config.heartbeat_seconds)
                await self.send(websocket, self.event("heartbeat"))

        async def controls() -> None:
            async for frame in websocket:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    message = json.loads(frame.data)
                except ValueError:
                    continue
                if isinstance(message, dict) and (response := await self.handle_control(message)) is not None:
                    await self.send(websocket, response)

        tasks = [asyncio.create_task(fn()) for fn in (deliver, heartbeat, controls)]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self.stopped.set()
        if self.websocket is not None:
            await self.websocket.close()
