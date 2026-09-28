"""One Claude Code session mirrored as one Discord Blue agent session.

The channel server runs inside the session's own process tree, so every event it
sees belongs to this session. A Discord reply becomes a channel notification,
which Claude Code queues as the next prompt. A tool permission prompt is relayed
to Discord only when Discord can show the whole request; Claude Code keeps the
terminal dialog open as well, and the first answer wins.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from discord_blue.agent_client import AgentSessionClient, Json, Rejected
from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.codex_bridge.session import REPLY_LIMIT, command_argv
from discord_blue.doodads.agent_session.protocol import approval_fits_discord

logger = logging.getLogger(__name__)

CAPABILITIES = ["approval_decision", "reply", "status_request"]
CHANNEL = "notifications/claude/channel"
PERMISSION = "notifications/claude/channel/permission"
PERMISSION_REQUEST = "notifications/claude/channel/permission_request"
# Claude Code shortens a long preview field around this marker, or replaces an unserializable one.
PREVIEW_CUTS = ("code points elided", "(value unserializable)")
# Bash arguments that change nothing an approval grants; any other field keeps the full preview shown.
BASH_DISPLAY_FIELDS = frozenset({"command", "description", "timeout"})
WAITING_LOCALLY = "Waiting on a permission decision in the Claude Code terminal"
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


def approval_command(tool_name: str, preview: str) -> list[str] | None:
    """The approval as Discord shows it, or None when Discord cannot show the whole request."""
    if not tool_name or any(cut in preview for cut in PREVIEW_CUTS):
        return None
    argv = [tool_name, preview]
    if tool_name == "Bash":
        try:
            fields = json.loads(preview)
        except ValueError:
            fields = None
        if isinstance(fields, dict) and isinstance(fields.get("command"), str) and set(fields) <= BASH_DISPLAY_FIELDS:
            argv = command_argv(fields["command"])
    return argv if approval_fits_discord(argv) else None


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
        request_id = str(params.get("request_id") or "")
        if not request_id or request_id in self.prompts:
            return
        tool_name, preview = str(params.get("tool_name") or ""), str(params.get("input_preview") or "")
        command = approval_command(tool_name, preview)
        if command is None:
            self.publish("status_changed", message=WAITING_LOCALLY)
            return
        description = str(params.get("description") or "")
        self.prompts[request_id] = self.event(
            "approval_request",
            approval_id=request_id,
            call_id=request_id,
            turn_id="",
            command=command,
            cwd=self.identity.cwd,
            reason=f"{tool_name}: {description}" if description else tool_name,
        )
        self.enqueue(self.prompts[request_id])

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
        try:
            await self.notify(PERMISSION, {"request_id": approval_id, "behavior": behavior})
        except OSError:
            return {**reject, "reason": LOST_CLAUDE}
        return self.event("approval_decision_ack", approval_id=approval_id)
