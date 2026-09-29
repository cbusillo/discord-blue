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
are held until Claude Code reports it is idle. When the conversation ends or changes, the session
takes a new epoch, so Discord controls meant for the old conversation are
rejected.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from discord_blue.agent_client import DEFERRED, PROMPT_EVENTS, AgentSessionClient, Json, Rejected
from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.claude_channel.launch import ancestry, loaded_as_channel
from discord_blue.codex_bridge.session import REPLY_LIMIT, TEXT_LIMIT, TURN_DONE
from discord_blue.claude_channel.transcript import TranscriptTitles
from discord_blue.session_titles import SessionLabel, typed_prompt

logger = logging.getLogger(__name__)

CAPABILITIES = ["reply", "status_request"]
CHANNEL = "notifications/claude/channel"
PERMISSION_REQUEST = "notifications/claude/channel/permission_request"
WAITING_LOCALLY = "Claude Code is waiting for approval in the terminal"
# Tool names Discord may show: built-in names and MCP names such as mcp__server__tool.
TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
LOST_CLAUDE = "Lost the Claude Code session, so delivery is uncertain. Check the terminal before retrying."
HOOK_TOOL = {
    "name": "dui_hook_event",
    "description": (
        "Internal plumbing for the dui plugin's hooks, which mirror this session into Discord. "
        "Never call this tool; calls from the model are ignored."
    ),
    "inputSchema": {"type": "object", "properties": {"event": {"type": "string"}}, "required": ["event"]},
}
# Hooks that only fire while Claude is working. Another plugin's Stop hook can keep a turn going after Stop.
RUNNING_EVENTS = {"UserPromptSubmit", "PreToolUse", "PostToolUse"}
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


def waiting_message(params: Json) -> str:
    tool_name = params.get("tool_name")
    shown = tool_name if isinstance(tool_name, str) and TOOL_NAME.fullmatch(tool_name) else "a tool"
    return f"{WAITING_LOCALLY} ({shown})"


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
        # session_title in hook input is a user-set name (-n or /rename); 2.1.284 exposes no separate auto title.
        self.label = SessionLabel()
        self.transcript = TranscriptTitles()
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
            harness="claude",
        )
        optional = {"branch": self.identity.branch, "title": self.label.current}
        return {**hello, **{key: value for key, value in optional.items() if value}}

    # Claude Code -> Discord

    async def on_permission_request(self, params: Json) -> None:
        # Never answered from here: the terminal dialog is the only place to approve or deny. Nothing else
        # from the request reaches Discord: Claude Code leaves some secrets in its preview unmasked.
        self.publish("status_changed", message=waiting_message(params))
        # The thread also gets it as a message, since a status only changes the reaction.
        self.publish("notice", message=waiting_message(params))

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
                self.conversation_id, self.label = conversation, SessionLabel()
                self.publish("notice", message=f"This Claude Code session is now on conversation `{conversation}`.")
            else:
                await self.switch_conversation(conversation)
        if event in RUNNING_EVENTS:
            self.turn_running = True
        if event == "UserPromptSubmit":
            prompt = fields.get("prompt", "")
            echo = prompt.lstrip().startswith("<channel") and any(f'command_id="{c}"' in prompt for c in self.injected)
            # Claude Code also fires this hook for prompts it injects itself, such as task notifications.
            typed = None if echo else typed_prompt(prompt)
            await self.retitle(fields, prompt=typed)
            if typed is not None and typed.strip():
                self.publish("user_message", message=clip(typed))
        elif event == "Stop":
            self.publish(
                "turn_complete", message=TURN_DONE, assistant_message=clip(fields.get("last_assistant_message", "")) or None
            )
            # Claude Code writes its own title for the session as it answers.
            await self.retitle(fields)
            # Not idle yet: another plugin's Stop hook may still keep Claude working.
        elif event == "StopFailure":
            self.publish("error", message="Claude Code ended the turn with an error; check the terminal.")
        elif event == "Notification":
            # idle_prompt: Claude Code has been waiting for input for about a minute, the only confirmed idle signal.
            await self.release_held()
        elif event == "SessionStart":
            await self.retitle(fields)

    async def switch_conversation(self, conversation: str | None) -> None:
        """Start a new epoch so Discord controls meant for the previous conversation are rejected."""
        self.epoch = uuid.uuid4().hex
        self.commands.clear()
        self.prompts.clear()
        dropped, self.held, self.turn_running = len(self.held), [], False
        self.conversation_id, self.label = conversation, SessionLabel()
        if self.websocket is not None:
            # Reconnect so Discord Blue binds the thread to the new epoch; nothing more goes out on this socket.
            await self.websocket.close()
        # Mirrored events still queued belong in the thread; prompts for the old conversation do not.
        self.outbox = deque({**event, "session_epoch": self.epoch} for event in self.outbox if event["type"] not in PROMPT_EVENTS)
        ended = "The Claude Code conversation in this thread ended (/clear or /resume)"
        self.publish("notice", message=f"{ended}; earlier replies from Discord are no longer accepted.")
        if dropped:
            waiting = f"{dropped} Discord {'reply was' if dropped == 1 else 'replies were'} waiting for Claude Code to be idle"
            self.publish(
                "notice", message=f"{waiting} and {'was' if dropped == 1 else 'were'} not delivered. Send again if still needed."
            )

    async def retitle(self, fields: dict[str, str], prompt: str | None = None) -> None:
        """Name first (hook session_title, else the transcript's custom title), then Claude's aiTitle, then the prompt."""
        titles = await self.transcript.read(fields.get("transcript_path", ""))
        name = fields.get("session_title") or titles.custom
        if (title := self.label.update(name=name, auto=titles.ai, prompt=prompt)) is not None:
            self.publish("title_changed", title=title)

    # Discord -> Claude Code

    async def inject(self, command_id: str, text: str) -> None:
        self.injected.append(command_id)
        # The injected message starts a turn; later replies wait for it to end.
        self.turn_running = True
        await self.notify(CHANNEL, {"content": text, "meta": {"command_id": command_id}})

    async def release_held(self) -> None:
        """Claude Code is confirmed idle: deliver the oldest held reply, which starts a turn; the rest wait.

        Delivering more at once would leave them in Claude Code's own queue, where /clear or /resume
        could carry them into the next conversation.
        """
        self.turn_running = False
        if not self.held:
            return
        command_id, text = self.held.pop(0)
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
