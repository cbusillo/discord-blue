"""One Claude Code session mirrored as one Discord Blue agent session.

The channel server runs inside the session's own process tree, so every event it
sees belongs to this session. A Discord reply becomes a channel notification,
which Claude Code queues as the next prompt.

Discord cannot answer Claude Code's permission prompts. The channel's permission
request carries only a lossy display preview and no tool-call ID, so a Discord
decision could not be bound reliably to the call it would allow. The server still
declares the permission capability, which leaves the terminal dialog unchanged,
so it can tell Discord that the session is waiting on the terminal.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from discord_blue.agent_client import AgentSessionClient, Json, Rejected
from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.codex_bridge.session import REPLY_LIMIT

logger = logging.getLogger(__name__)

CAPABILITIES = ["reply", "status_request"]
CHANNEL = "notifications/claude/channel"
PERMISSION_REQUEST = "notifications/claude/channel/permission_request"
WAITING_LOCALLY = "Claude Code is waiting for approval in the terminal"
# Tool names Discord may show: built-in names and MCP names such as mcp__server__tool.
TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
LOST_CLAUDE = "Lost the Claude Code session, so delivery is uncertain. Check the terminal before retrying."

Notify = Callable[[str, Json], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Identity:
    session_id: str
    cwd: str
    branch: str | None
    pid: int

    @classmethod
    def from_environment(cls) -> Identity | None:
        """The session Claude Code started this server for, or None outside Claude Code."""
        session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
        if not session_id:
            return None
        cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        return cls(session_id=session_id, cwd=cwd, branch=git_branch(cwd), pid=os.getppid())


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


class ClaudeSession(AgentSessionClient):
    capabilities = CAPABILITIES
    command_errors = (OSError,)

    def __init__(self, config: BridgeConfig, identity: Identity, notify: Notify) -> None:
        super().__init__(config, identity.session_id)
        self.identity = identity
        self.notify = notify
        self.title: str | None = None

    def hello(self, *, first: bool) -> Json:
        hello = self.event(
            "hello",
            host_label=self.config.host_label,
            cwd=self.identity.cwd,
            pid=self.identity.pid,
            capabilities=self.capabilities,
        )
        optional = {"branch": self.identity.branch, "title": self.title}
        return {**hello, **{key: value for key, value in optional.items() if value}}

    # Claude Code -> Discord

    async def on_permission_request(self, params: Json) -> None:
        # Never answered from here: the terminal dialog is the only place to approve or deny. Nothing else
        # from the request reaches Discord: Claude Code leaves some secrets in its preview unmasked.
        self.publish("status_changed", message=waiting_message(params))

    # Discord -> Claude Code

    async def run_command(self, message: Json) -> None:
        kind = message.get("kind")
        if kind == "reply":
            text = message.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > REPLY_LIMIT:
                raise Rejected(f"Replies must contain 1 to {REPLY_LIMIT} characters.")
            meta = {"command_id": str(message["command_id"])}
            await self.notify(CHANNEL, {"content": text, "meta": meta})
        elif kind == "status_request":
            self.enqueue(self.status_snapshot())
        else:
            raise Rejected("Claude Code channels cannot do this; use the Claude Code terminal.")

    def failure_reason(self, exc: Exception) -> str:
        return LOST_CLAUDE
