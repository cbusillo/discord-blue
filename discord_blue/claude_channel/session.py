"""One Claude Code session mirrored as one Discord Blue agent session.

The channel server runs inside the session's own process tree, so every event it
sees belongs to this session. A Discord reply becomes a channel notification,
which Claude Code queues as the next prompt. A tool permission prompt is relayed
to Discord only when this server knows the exact tool call, the directory it
runs in, and that Discord can show both in full. Claude Code keeps the terminal
dialog open as well, and the first answer wins.

The plugin's hooks call ``dui_hook_event`` on this server to mirror typed
prompts, final answers and titles, and to report each tool call before it runs
(its exact input, ``tool_use_id`` and working directory). A permission request
is matched to exactly one such call or stays local. Claude Code never says when
the terminal answered a relayed prompt, so a prompt is retired from Discord when
its tool call finishes, the next prompt arrives, or the turn ends. When the
conversation ends or changes (``/clear``, ``/resume``), the session takes a new
epoch, so Discord controls meant for the old conversation are rejected.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import socket
import subprocess
import uuid
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from discord_blue.agent_client import DEFERRED, PROMPT_EVENTS, AgentSessionClient, Json, Rejected
from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.claude_channel.launch import ancestry, loaded_as_channel
from discord_blue.codex_bridge.session import REPLY_LIMIT, TEXT_LIMIT, TURN_DONE, command_argv, thread_title
from discord_blue.doodads.agent_session.protocol import approval_fits_discord

logger = logging.getLogger(__name__)

CAPABILITIES = ["approval_decision", "reply", "status_request"]
CHANNEL = "notifications/claude/channel"
PERMISSION = "notifications/claude/channel/permission"
PERMISSION_REQUEST = "notifications/claude/channel/permission_request"
# Bash arguments that change nothing an approval grants; any other field keeps the full preview shown.
BASH_DISPLAY_FIELDS = frozenset({"command", "description", "timeout"})
WAITING_LOCALLY = "Waiting on a permission decision in the Claude Code terminal"
LOST_CLAUDE = "Lost the Claude Code session, so delivery is uncertain. Check the terminal before retrying."
HOOK_TOOL = {
    "name": "dui_hook_event",
    "description": (
        "Internal plumbing for the dui plugin's hooks, which mirror this session into Discord. "
        "Never call this tool; calls from the model are ignored."
    ),
    "inputSchema": {"type": "object", "properties": {"event": {"type": "string"}}, "required": ["event"]},
}
TOOL_EVENTS = {"PostToolUse", "PostToolUseFailure"}
TURN_EVENTS = {"UserPromptSubmit", "Stop", "StopFailure"}
TOOL_CALL_MEMORY = 64
# What Claude Code puts in a permission preview in place of a masked credential, a shortened field, or a
# value it cannot serialize (channels reference, "Permission request fields").
LOSSY_PREVIEW_MARKERS = ("[REDACTED]", "code points elided", "(value unserializable)")
# The `_meta` key Claude Code sets on a model's tool call; hook calls carry no tool use.
MODEL_CALL = "claudecode/toolUseId"

Notify = Callable[[str, Json], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Identity:
    session_id: str
    cwd: str
    branch: str | None
    pid: int
    # False when the session was launched without the development-channels flag for this plugin.
    channel: bool = True

    @classmethod
    def from_environment(cls) -> Identity | None:
        """The session Claude Code started this server for, or None outside Claude Code."""
        session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
        if not session_id:
            return None
        cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        channel = loaded_as_channel(ancestry(os.getpid())) is not False
        return cls(session_id=session_id, cwd=cwd, branch=git_branch(cwd), pid=os.getppid(), channel=channel)


def git_branch(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def host_label() -> str:
    return f"Claude Code on {socket.gethostname().split('.')[0]}"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool call as the hooks reported it: exactly what will run, and where."""

    tool_use_id: str
    tool_name: str
    tool_input: Json
    cwd: str


def approval_command(tool_name: str, tool_input: Json) -> list[str] | None:
    """The argv Discord shows for this call, rendered shell-joined, or None when that would misstate it."""
    command = tool_input.get("command")
    if tool_name == "Bash" and isinstance(command, str) and set(tool_input) <= BASH_DISPLAY_FIELDS:
        argv = command_argv(command)
        # Re-quoting can change what the shell does ("$(...)" would show as a literal), so show a
        # command only when Discord's rendering of it is its exact source.
        return argv if shlex.join(argv) == command else None
    # Anything else is shown as data: the tool name and its complete JSON input.
    return [tool_name, json.dumps(tool_input, ensure_ascii=False)] if tool_name else None


def sanitizer_proof(value: object) -> bool:
    """Whether Claude Code's preview sanitizer leaves this value unchanged, so an equal preview proves it.

    The sanitizer folds whitespace runs and neutralizes invisible, direction-override and lookalike
    characters; plain printable ASCII with single spaces is the text it provably keeps.
    """
    if isinstance(value, str):
        return all(" " <= char <= "~" for char in value) and "  " not in value
    if isinstance(value, list):
        return all(sanitizer_proof(item) for item in value)
    if isinstance(value, dict):
        return all(sanitizer_proof(key) and sanitizer_proof(item) for key, item in value.items())
    return value is None or isinstance(value, bool | int | float)


def unflagged_notice(session_id: str) -> str:
    return (
        "This Claude Code session was started without the dui channel, so Discord replies and approvals cannot reach it; "
        "prompts and answers are still mirrored here. To enable them, exit and run "
        f"`claude --resume {session_id} --dangerously-load-development-channels plugin:dui@discord-blue`."
    )


def clip(text: str) -> str:
    return text[:TEXT_LIMIT] + "\n[Truncated; see the Claude Code terminal.]" if len(text) > TEXT_LIMIT else text


class ClaudeSession(AgentSessionClient):
    command_errors = (OSError,)

    def __init__(self, config: BridgeConfig, identity: Identity, notify: Notify) -> None:
        super().__init__(config, identity.session_id)
        self.identity = identity
        self.notify = notify
        self.title: str | None = None
        self.conversation_id: str | None = identity.session_id
        self.controls = CAPABILITIES if identity.channel else ["status_request"]
        # What each relayed prompt asked for, so the tool call that answers it can retire it.
        self.approval_calls: dict[str, str] = {}
        # Tool calls reported by PreToolUse that have not finished, by tool_use_id.
        self.tool_calls: OrderedDict[str, ToolCall] = OrderedDict()
        # Matched permission requests waiting for the PermissionRequest hook, and final inputs it reported.
        self.awaiting: dict[str, tuple[ToolCall, list[str], Json]] = {}
        self.final_inputs: dict[str, object] = {}
        # Discord replies held while a turn runs: Claude Code queues channel messages and would deliver
        # them even after /clear or /resume switched conversations.
        self.turn_running = False
        self.held: list[tuple[str, str]] = []
        # Replies this server injected; their prompts are already in Discord.
        self.injected: deque[str] = deque(maxlen=64)
        if not identity.channel:
            self.publish("notice", message=unflagged_notice(identity.session_id))

    def hello(self, *, first: bool) -> Json:
        hello = self.event(
            "hello",
            host_label=self.config.host_label,
            cwd=self.identity.cwd,
            pid=self.identity.pid,
            capabilities=self.controls,
        )
        optional = {"branch": self.identity.branch, "title": self.title}
        return {**hello, **{key: value for key, value in optional.items() if value}}

    # Claude Code -> Discord

    async def on_permission_request(self, params: Json) -> None:
        request_id = str(params.get("request_id") or "")
        if not request_id or request_id in self.prompts or request_id in self.awaiting:
            return
        tool_name, preview = str(params.get("tool_name") or ""), str(params.get("input_preview") or "")
        call = self.tool_call_for(request_id, tool_name, preview)
        command = approval_command(tool_name, call.tool_input) if call is not None else None
        if call is None or command is None or not approval_fits_discord(command, call.cwd):
            self.publish("status_changed", message=WAITING_LOCALLY)
            return
        # Another PreToolUse hook may have rewritten the input; wait for PermissionRequest to report the final one.
        self.awaiting[request_id] = (call, command, params)
        self.confirm_final_input(tool_name)

    def confirm_final_input(self, tool_name: str) -> None:
        """Relay a matched request once the PermissionRequest hook shows its final input is the one reported."""
        waiting = [request_id for request_id, (call, _, _) in self.awaiting.items() if call.tool_name == tool_name]
        if not waiting or tool_name not in self.final_inputs:
            return
        final = self.final_inputs.pop(tool_name)
        for request_id in waiting:
            call, command, params = self.awaiting.pop(request_id)
            if final == call.tool_input:
                self.relay(request_id, call, command, params)
            else:
                self.publish("status_changed", message=WAITING_LOCALLY)

    def relay(self, request_id: str, call: ToolCall, command: list[str], params: Json) -> None:
        tool_name = call.tool_name
        description = str(params.get("description") or "")
        self.approval_calls[request_id] = call.tool_use_id
        self.prompts[request_id] = self.event(
            "approval_request",
            approval_id=request_id,
            call_id=request_id,
            turn_id="",
            command=command,
            cwd=call.cwd,
            reason=f"{tool_name}: {description}" if description else tool_name,
        )
        self.enqueue(self.prompts[request_id])

    def tool_call_for(self, request_id: str, tool_name: str, preview: str) -> ToolCall | None:
        """The one unfinished tool call this permission request is for, or None when that is not certain.

        The request carries only a display preview, and Claude Code's preview is lossy: it masks credentials,
        shortens long fields, folds whitespace and neutralizes lookalike characters. So a preview carrying a
        masking or shortening marker never matches; the preview must equal the reported input exactly, and
        that input must be one the sanitizer cannot have changed; and any other unfinished call to the same
        tool could have produced the same preview, so the request is relayed only when exactly one exists.
        """
        if any(marker in preview for marker in LOSSY_PREVIEW_MARKERS):
            return None
        try:
            shown = json.loads(preview)
        except ValueError:
            return None
        calls = [call for call in self.tool_calls.values() if call.tool_name == tool_name]
        if len(calls) != 1 or calls[0].tool_use_id in self.approval_calls.values():
            return None
        return calls[0] if calls[0].tool_input == shown and sanitizer_proof(shown) else None

    def retire_approvals(self, tool_use_id: str | None = None) -> None:
        """Retire relayed prompts the terminal may have answered: all of them, or the one for a finished call."""
        for request_id, asked in list(self.approval_calls.items()):
            if tool_use_id is not None and asked != tool_use_id:
                continue
            del self.approval_calls[request_id]
            if self.prompts.pop(request_id, None) is not None:
                self.publish("approval_resolved", approval_id=request_id)

    async def on_hook_call(self, arguments: Json, meta: Json) -> str:
        if MODEL_CALL in meta:
            return "Ignored: this tool is internal to the dui plugin's hooks."
        # A field the hook input lacks may arrive as its unsubstituted placeholder.
        fields = {key: value for key, value in arguments.items() if isinstance(value, str) and not value.startswith("${")}
        await self.on_hook(fields)
        # Hook output text becomes context for the model, so always return none.
        return ""

    async def on_hook(self, fields: dict[str, str]) -> None:
        event = fields.get("event")
        conversation = fields.get("session_id") or None
        if event == "SessionEnd":
            # /clear or /resume ends this conversation; the next hook names the one that follows.
            await self.switch_conversation(None)
            return
        if conversation and conversation != self.conversation_id:
            if self.conversation_id is None:
                self.conversation_id, self.title = conversation, None
                self.publish("notice", message=f"This Claude Code session is now on conversation `{conversation}`.")
            else:
                await self.switch_conversation(conversation)
        if event in TURN_EVENTS:
            self.retire_approvals()
        if event == "UserPromptSubmit":
            # A new turn: calls from an interrupted one (no PostToolUse or Stop) can no longer run.
            self.forget_calls()
            self.turn_running = True
            prompt = fields.get("prompt", "")
            echo = prompt.lstrip().startswith("<channel") and any(f'command_id="{c}"' in prompt for c in self.injected)
            self.retitle(fields.get("session_title") or self.title or ("" if echo else prompt))
            if prompt.strip() and not echo:
                self.publish("user_message", message=clip(prompt))
        elif event == "Stop":
            self.forget_calls()
            self.publish(
                "turn_complete", message=TURN_DONE, assistant_message=clip(fields.get("last_assistant_message", "")) or None
            )
            await self.release_held()
        elif event == "StopFailure":
            self.forget_calls()
            self.publish("error", message="Claude Code ended the turn with an error; check the terminal.")
            await self.release_held()
        elif event == "Notification":
            # idle_prompt: Claude Code is waiting for input, which also covers a turn interrupted without Stop.
            self.forget_calls()
            await self.release_held()
        elif event == "PermissionRequest":
            try:
                self.final_inputs[fields.get("tool_name", "")] = json.loads(fields.get("tool_input", ""))
            except ValueError:
                return
            self.confirm_final_input(fields.get("tool_name", ""))
        elif event == "PreToolUse":
            self.on_tool_call(fields)
        elif event in TOOL_EVENTS:
            tool_use_id = fields.get("tool_use_id", "")
            self.tool_calls.pop(tool_use_id, None)
            if tool_use_id:
                self.retire_approvals(tool_use_id)
        elif event == "SessionStart":
            self.retitle(fields.get("session_title") or "")

    def forget_calls(self) -> None:
        self.tool_calls.clear()
        self.awaiting.clear()
        self.final_inputs.clear()

    def on_tool_call(self, fields: dict[str, str]) -> None:
        try:
            tool_input = json.loads(fields.get("tool_input", ""))
        except ValueError:
            return
        tool_use_id, tool_name, cwd = fields.get("tool_use_id"), fields.get("tool_name"), fields.get("cwd")
        if not (tool_use_id and tool_name and cwd and isinstance(tool_input, dict)):
            return
        self.tool_calls[tool_use_id] = ToolCall(tool_use_id, tool_name, tool_input, cwd)
        while len(self.tool_calls) > TOOL_CALL_MEMORY:
            self.tool_calls.popitem(last=False)

    async def switch_conversation(self, conversation: str | None) -> None:
        """Start a new epoch so Discord controls meant for the previous conversation are rejected."""
        self.epoch = uuid.uuid4().hex
        self.commands.clear()
        self.prompts.clear()
        self.approval_calls.clear()
        self.forget_calls()
        dropped, self.held, self.turn_running = len(self.held), [], False
        self.conversation_id, self.title = conversation, None
        if self.websocket is not None:
            # Reconnect so Discord Blue binds the thread to the new epoch; nothing more goes out on this socket.
            await self.websocket.close()
        # Mirrored events still queued belong in the thread; prompts for the old conversation do not.
        self.outbox = deque({**event, "session_epoch": self.epoch} for event in self.outbox if event["type"] not in PROMPT_EVENTS)
        ended = "The Claude Code conversation in this thread ended (/clear or /resume)"
        self.publish("notice", message=f"{ended}; earlier replies and approvals from Discord are no longer accepted.")
        if dropped:
            waiting = f"{dropped} Discord {'reply was' if dropped == 1 else 'replies were'} waiting for the turn to end"
            self.publish("notice", message=f"{waiting} and not delivered. Send again if still needed.")

    def retitle(self, text: str) -> None:
        title = thread_title({"name": text})
        if title and title != self.title:
            self.title = title
            self.publish("title_changed", title=title)

    # Discord -> Claude Code

    async def inject(self, command_id: str, text: str) -> None:
        self.injected.append(command_id)
        # The injected message starts a turn; later replies wait for it to end.
        self.turn_running = True
        await self.notify(CHANNEL, {"content": text, "meta": {"command_id": command_id}})

    async def release_held(self) -> None:
        """Deliver replies held during the turn that just ended."""
        self.turn_running = False
        held, self.held = self.held, []
        for command_id, text in held:
            try:
                await self.inject(command_id, text)
            except OSError:
                self.finish_command(command_id, self.event("command_reject", command_id=command_id, reason=LOST_CLAUDE))
            else:
                self.finish_command(command_id, self.event("command_ack", command_id=command_id))

    async def run_command(self, message: Json) -> object:
        kind = message.get("kind")
        if kind == "reply":
            text = message.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > REPLY_LIMIT:
                raise Rejected(f"Replies must contain 1 to {REPLY_LIMIT} characters.")
            command_id = str(message["command_id"])
            if self.turn_running:
                self.held.append((command_id, text))
                return DEFERRED
            await self.inject(command_id, text)
        elif kind == "status_request":
            self.enqueue(self.status_snapshot())
        else:
            raise Rejected("Claude Code channels cannot do this; use the Claude Code terminal.")
        return None

    def failure_reason(self, exc: Exception) -> str:
        return LOST_CLAUDE

    async def approval_decision(self, message: Json) -> Json:
        approval_id = str(message.get("approval_id") or "")
        behavior = {"approved": "allow", "denied": "deny"}.get(str(message.get("decision")))
        reject = self.event("approval_decision_reject", approval_id=approval_id)
        if not self.is_current(message):
            return {**reject, "reason": "Stale session; the decision was not sent."}
        if approval_id not in self.prompts or behavior is None:
            return {**reject, "reason": "This approval is no longer pending."}
        # Claude Code never says whether the terminal answered first, so the request is forgotten here.
        del self.prompts[approval_id]
        tool_use_id = self.approval_calls.pop(approval_id, None)
        if behavior == "deny" and tool_use_id is not None:
            # A denied call never runs, so no PostToolUse would forget it.
            self.tool_calls.pop(tool_use_id, None)
        try:
            await self.notify(PERMISSION, {"request_id": approval_id, "behavior": behavior})
        except OSError:
            return {**reject, "reason": LOST_CLAUDE}
        return self.event("approval_decision_ack", approval_id=approval_id)
