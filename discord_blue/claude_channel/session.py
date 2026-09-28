"""One Claude Code session mirrored as one Discord Blue agent session.

The channel server runs inside the session's own process tree, so every event it
sees belongs to this session. A Discord reply becomes a channel notification,
which Claude Code queues as the next prompt. A tool permission prompt is relayed
to Discord only when this server knows the exact tool call, the directory it
runs in, and that Discord can show both in full. Claude Code keeps the terminal
dialog open as well, and the first answer wins.

The plugin's hooks call ``dui_hook_event`` on this server to mirror typed
prompts, final answers and titles. Claude Code never says when the terminal
answered a relayed prompt, so a prompt is retired from Discord when the tool
runs, the next prompt arrives, or the turn ends.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import socket
import subprocess
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from discord_blue.agent_client import AgentSessionClient, Json, Rejected
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


def preview_command(tool_name: str, preview: str) -> str | None:
    """The shell command a Bash preview carries, to match against the tool call that later runs."""
    if tool_name != "Bash":
        return None
    try:
        fields = json.loads(preview)
    except ValueError:
        return None
    command = fields.get("command") if isinstance(fields, dict) else None
    return " ".join(command.split()) if isinstance(command, str) else None


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
        self.conversation_id = identity.session_id
        self.controls = CAPABILITIES if identity.channel else ["status_request"]
        # What each relayed prompt asked for, so the tool call that answers it can retire it.
        self.approval_calls: dict[str, tuple[str, str | None]] = {}
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
        if not request_id or request_id in self.prompts:
            return
        tool_name, preview = str(params.get("tool_name") or ""), str(params.get("input_preview") or "")
        call = self.tool_call_for(request_id, tool_name, preview)
        command = approval_command(tool_name, call.tool_input) if call is not None else None
        if call is None or command is None or not approval_fits_discord(command, call.cwd):
            self.publish("status_changed", message=WAITING_LOCALLY)
            return
        description = str(params.get("description") or "")
        self.approval_calls[request_id] = (tool_name, preview_command(tool_name, preview))
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
        """The one tool call this permission request is for, or None when that is not certain.

        The request carries only a display preview, and Claude Code keeps a `cd` between Bash calls, so
        the command and its directory must come from hooks. Without them every request stays local.
        """
        return None

    def retire_approvals(self, tool_name: str | None = None, command: str | None = None) -> None:
        """Retire relayed prompts the terminal may have answered: all of them, or those for one tool call."""
        for request_id, (asked_tool, asked_command) in list(self.approval_calls.items()):
            if tool_name is not None and (tool_name != asked_tool or asked_command not in (None, command)):
                continue
            del self.approval_calls[request_id]
            if self.prompts.pop(request_id, None) is not None:
                self.publish("approval_resolved", approval_id=request_id)

    async def on_hook_call(self, arguments: Json, meta: Json) -> str:
        if MODEL_CALL in meta:
            return "Ignored: this tool is internal to the dui plugin's hooks."
        # A field the hook input lacks may arrive as its unsubstituted placeholder.
        fields = {key: value for key, value in arguments.items() if isinstance(value, str) and not value.startswith("${")}
        self.on_hook(fields)
        # Hook output text becomes context for the model, so always return none.
        return ""

    def on_hook(self, fields: dict[str, str]) -> None:
        event = fields.get("event")
        if (conversation := fields.get("session_id")) and conversation != self.conversation_id:
            # /clear or /resume switched conversations inside this process, which keeps its channel and thread.
            self.conversation_id, self.title = conversation, None
            self.publish("notice", message=f"This Claude Code session switched to conversation `{conversation}`.")
        if event == "UserPromptSubmit":
            self.retire_approvals()
            prompt = fields.get("prompt", "")
            echo = prompt.lstrip().startswith("<channel") and any(f'command_id="{c}"' in prompt for c in self.injected)
            self.retitle(fields.get("session_title") or self.title or ("" if echo else prompt))
            if prompt.strip() and not echo:
                self.publish("user_message", message=clip(prompt))
        elif event == "Stop":
            self.retire_approvals()
            self.publish(
                "turn_complete", message=TURN_DONE, assistant_message=clip(fields.get("last_assistant_message", "")) or None
            )
        elif event == "StopFailure":
            self.retire_approvals()
            self.publish("error", message="Claude Code ended the turn with an error; check the terminal.")
        elif event in TOOL_EVENTS:
            command = fields.get("command")
            self.retire_approvals(fields.get("tool_name", ""), " ".join(command.split()) if command else None)
        elif event == "SessionStart":
            self.retitle(fields.get("session_title") or "")

    def retitle(self, text: str) -> None:
        title = thread_title({"name": text})
        if title and title != self.title:
            self.title = title
            self.publish("title_changed", title=title)

    # Discord -> Claude Code

    async def run_command(self, message: Json) -> None:
        kind = message.get("kind")
        if kind == "reply":
            text = message.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > REPLY_LIMIT:
                raise Rejected(f"Replies must contain 1 to {REPLY_LIMIT} characters.")
            command_id = str(message["command_id"])
            self.injected.append(command_id)
            await self.notify(CHANNEL, {"content": text, "meta": {"command_id": command_id}})
        elif kind == "status_request":
            self.enqueue(self.status_snapshot())
        else:
            raise Rejected("Claude Code channels cannot do this; use the Claude Code terminal.")

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
        self.approval_calls.pop(approval_id, None)
        try:
            await self.notify(PERMISSION, {"request_id": approval_id, "behavior": behavior})
        except OSError:
            return {**reject, "reason": LOST_CLAUDE}
        return self.event("approval_decision_ack", approval_id=approval_id)
