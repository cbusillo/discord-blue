"""One Claude Code session mirrored as one Discord Blue agent session.

The channel server runs inside the session's own process tree, so every event it
sees belongs to this session. A Discord reply becomes a channel notification,
which Claude Code queues as the next prompt.

Discord cannot answer Claude Code's permission prompts. The channel's permission
request carries only a lossy display preview and no tool-call ID, so a Discord
decision could not be bound reliably to the call it would allow. The server still
declares the permission capability, which leaves the terminal dialog unchanged,
so it can post Claude Code's own (credential-masked) preview as a notice.

The plugin's hooks call ``dui_hook_event`` on this server to mirror typed
prompts, final answers and titles. Claude Code queues channel messages while a
turn runs and keeps them across ``/clear`` and ``/resume``, so Discord replies
are held until the turn ends. When the conversation ends or changes, the session
takes a new epoch, so Discord controls meant for the old conversation are
rejected.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from discord_blue.agent_client import DEFERRED, PROMPT_EVENTS, AgentSessionClient, Json, Rejected
from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.claude_channel.launch import ancestry, loaded_as_channel
from discord_blue.codex_bridge.session import REPLY_LIMIT, TEXT_LIMIT, TURN_DONE, thread_title

logger = logging.getLogger(__name__)

CAPABILITIES = ["reply", "status_request"]
CHANNEL = "notifications/claude/channel"
PERMISSION_REQUEST = "notifications/claude/channel/permission_request"
WAITING_LOCALLY = "Claude Code is waiting for approval in the terminal"
PREVIEW_LIMIT = 1200
LOST_CLAUDE = "Lost the Claude Code session, so delivery is uncertain. Check the terminal before retrying."
HOOK_TOOL = {
    "name": "dui_hook_event",
    "description": (
        "Internal plumbing for the dui plugin's hooks, which mirror this session into Discord. "
        "Never call this tool; calls from the model are ignored."
    ),
    "inputSchema": {"type": "object", "properties": {"event": {"type": "string"}}, "required": ["event"]},
}
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


def permission_notice(params: Json) -> str:
    """Tell Discord a permission prompt is open, showing Claude Code's preview as a preview only."""
    tool_name = str(params.get("tool_name") or "a tool").replace("`", "")
    description = str(params.get("description") or "")[:300]
    # Claude Code masks credentials in this preview; a fence inside it would end the block early.
    preview = str(params.get("input_preview") or "").replace("```", "`\u200b``")
    if len(preview) > PREVIEW_LIMIT:
        preview = preview[:PREVIEW_LIMIT] + " …"
    lines = [f"Claude is waiting for approval in the terminal: `{tool_name}`", *([description] if description else [])]
    lines.append("Preview from Claude Code (approve or deny in the terminal; Discord cannot):")
    return "\n".join([*lines, f"```\n{preview}\n```"])


def unflagged_notice(session_id: str) -> str:
    return (
        "This Claude Code session was started without the dui channel, so Discord replies cannot reach it; "
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
        # Never answered from here: the terminal dialog is the only place to approve or deny.
        self.publish("status_changed", message=f"{WAITING_LOCALLY} ({params.get('tool_name') or 'a tool'})")
        self.publish("notice", message=permission_notice(params))

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
        if event == "UserPromptSubmit":
            self.turn_running = True
            prompt = fields.get("prompt", "")
            echo = prompt.lstrip().startswith("<channel") and any(f'command_id="{c}"' in prompt for c in self.injected)
            self.retitle(fields.get("session_title") or self.title or ("" if echo else prompt))
            if prompt.strip() and not echo:
                self.publish("user_message", message=clip(prompt))
        elif event == "Stop":
            self.publish(
                "turn_complete", message=TURN_DONE, assistant_message=clip(fields.get("last_assistant_message", "")) or None
            )
            await self.release_held()
        elif event == "StopFailure":
            self.publish("error", message="Claude Code ended the turn with an error; check the terminal.")
            await self.release_held()
        elif event == "Notification":
            # idle_prompt: Claude Code is waiting for input, which also covers a turn interrupted without Stop.
            await self.release_held()
        elif event == "SessionStart":
            self.retitle(fields.get("session_title") or "")

    async def switch_conversation(self, conversation: str | None) -> None:
        """Start a new epoch so Discord controls meant for the previous conversation are rejected."""
        self.epoch = uuid.uuid4().hex
        self.commands.clear()
        self.prompts.clear()
        dropped, self.held, self.turn_running = len(self.held), [], False
        self.conversation_id, self.title = conversation, None
        if self.websocket is not None:
            # Reconnect so Discord Blue binds the thread to the new epoch; nothing more goes out on this socket.
            await self.websocket.close()
        # Mirrored events still queued belong in the thread; prompts for the old conversation do not.
        self.outbox = deque({**event, "session_epoch": self.epoch} for event in self.outbox if event["type"] not in PROMPT_EVENTS)
        ended = "The Claude Code conversation in this thread ended (/clear or /resume)"
        self.publish("notice", message=f"{ended}; earlier replies from Discord are no longer accepted.")
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
