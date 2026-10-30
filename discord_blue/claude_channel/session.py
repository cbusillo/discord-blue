"""One Claude Code session mirrored as one Discord Blue agent session.

The channel server runs inside the session's own process tree, so every event it
sees belongs to this session. A Discord reply becomes a channel notification,
which Claude Code queues as the next prompt.

Every terminal dialog, a permission prompt or a question Claude asks, reaches
this server as the plugin's PermissionRequest hook call, with the tool's exact
input. While the dialog is open the call waits: a Discord answer becomes the
hook's decision, so it can only apply to that dialog, and an allow carries the
exact input Discord showed. Anything Discord cannot show whole is answered in
the terminal. Claude Code drops a hook's answer once the terminal answered
first, and tells no hook when that answer was a rejection, so a Discord answer
first checks the transcript for the call's result.

The plugin's hooks call ``dui_hook_event`` on this server to mirror typed
prompts, final answers and titles. A Discord reply goes to Claude Code at once,
which queues it like a typed prompt while a turn runs. When the conversation ends or changes, the session
takes a new epoch, so Discord controls meant for the old conversation are
rejected.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import socket
import subprocess
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiohttp

from discord_blue.doodads.agent_session.protocol import (
    LAUNCH_BRIEF_ORIGIN,
    approval_content_displayable,
    command_text_displayable,
)
from discord_blue.agent_client import PROMPT_EVENTS, SESSION_END_TIMEOUT_SECONDS, AgentSessionClient, Json, Rejected
from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.claude_channel.launch import launch_prompt, matches_launch_prompt, ancestry, loaded_as_channel
from discord_blue.codex_bridge.session import REPLY_LIMIT, TEXT_LIMIT, TURN_DONE, command_argv
from discord_blue.claude_channel.transcript import TranscriptTitles, Titles
from discord_blue.session_titles import SessionLabel, typed_prompt

logger = logging.getLogger(__name__)

# Answering dialogs needs only the hooks; replies need the channel.
CAPABILITIES = ["reply", "status_request", "approval_decision", "request_user_input_response"]
DIALOG_CAPABILITIES = ["status_request", "approval_decision", "request_user_input_response"]
CHANNEL = "notifications/claude/channel"
WAITING_LOCALLY = "Claude Code is waiting on you in the terminal"
ANSWERED_IN_TERMINAL = "That was already answered in the Claude Code terminal."
CONTINUING = "Answered; continuing the turn."
QUESTION_TOOL = "AskUserQuestion"
# Bash fields a command card leaves unshown: Claude's own description is shown as the reason, and a timeout or
# running in the background changes how the command runs, not what it does. Any other field shows the whole input.
COMMAND_FIELDS = frozenset({"command", "description", "timeout", "run_in_background"})
# PreToolUse calls remembered so a dialog can be tied to its tool call; dialogs follow their PreToolUse at once.
TOOL_CALL_MEMORY = 64
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
# A final answer waits for the other Stop hooks' verdict. Claude going idle, a new prompt or the session ending
# posts it at once; this bound only covers a verdict never recorded, past the hooks' 600-second default timeout.
STOP_VERDICT_SECONDS = 660.0
STOP_VERDICT_POLL_SECONDS = 0.1
STOP_VERDICT_SLOW_POLL_SECONDS = 1.0
STOP_VERDICT_FAST_SECONDS = 5.0
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
    spawned: bool = False
    launch_prompt: str | None = None

    @classmethod
    def from_environment(cls) -> Identity | None:
        """The session Claude Code started this server for, or None outside Claude Code."""
        session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
        if not session_id:
            return None
        cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        command_lines = ancestry(os.getpid())
        channel = loaded_as_channel(command_lines) is not False
        return cls(
            session_id=session_id,
            cwd=cwd,
            branch=git_branch(cwd),
            pid=os.getppid(),
            channel=channel,
            spawned=os.environ.get("DISCORD_BLUE_SESSION_KIND") == "spawned",
            launch_prompt=launch_prompt(command_lines),
        )


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


def waiting_message(tool_name: str) -> str:
    shown = tool_name if TOOL_NAME.fullmatch(tool_name) else "a tool"
    return f"{WAITING_LOCALLY} ({shown})"


def json_object(text: str | None) -> Json | None:
    try:
        value = json.loads(text) if text else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def question_cards(questions: object) -> list[Json] | None:
    """AskUserQuestion's questions as Discord question cards; a multi-select question offers only its options."""
    if not isinstance(questions, list) or not questions:
        return None
    cards = []
    for index, question in enumerate(questions):
        if not isinstance(question, dict) or not isinstance(question.get("question"), str):
            return None
        options = question.get("options") or []
        if not isinstance(options, list) or not all(isinstance(o, dict) and isinstance(o.get("label"), str) for o in options):
            return None
        multi = question.get("multiSelect") is True
        cards.append(
            {
                "id": f"q{index}",
                "header": str(question.get("header") or ""),
                "question": question["question"],
                "options": [{"label": o["label"], "description": str(o.get("description") or "")} for o in options],
                "isOther": not multi,
                "isSecret": False,
                "multiSelect": multi,
            }
        )
    return cards


def question_answers(questions: list[Json], response: object) -> Json | None:
    """Discord's answers keyed by question text, as AskUserQuestion takes them; None when any is missing."""
    raw = response.get("answers") if isinstance(response, dict) else None
    answers: Json = {}
    for index, question in enumerate(questions):
        value = raw.get(f"q{index}") if isinstance(raw, dict) else None
        picked = [str(a) for a in value.get("answers") or [] if str(a).strip()] if isinstance(value, dict) else []
        if not picked:
            return None
        answers[question["question"]] = picked if question.get("multiSelect") is True else picked[0]
    return answers


def hook_decision(decision: Json) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}})


@dataclass(slots=True)
class Dialog:
    """A terminal dialog whose PermissionRequest hook call waits on `answer`; Discord sets it with a decision."""

    tool_name: str
    tool_input: Json
    tool_use_id: str | None
    transcript_path: str
    offset: int | None
    answer: asyncio.Future[Json | None]


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
        self.launch_brief_pending = identity.spawned and identity.launch_prompt is not None
        self.pending_input: tuple[dict[str, str], str] | None = None
        self.ending: tuple[Json, asyncio.Event] | None = None
        self.conversation_id: str | None = identity.session_id
        self.controls = CAPABILITIES if identity.channel else DIALOG_CAPABILITIES
        # Open terminal dialogs by prompt key; the ones Discord can answer are also in self.prompts.
        self.dialogs: dict[str, Dialog] = {}
        self.tool_calls: deque[tuple[str, Json | None, str]] = deque(maxlen=TOOL_CALL_MEMORY)
        # Hooks run one at a time, as Claude Code sends them; only a dialog's wait for its answer runs alongside.
        self.hook_lock = asyncio.Lock()
        self.pending_stop: asyncio.Task[None] | None = None
        self.settle = asyncio.Event()
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
            session_kind="spawned" if self.identity.spawned else "director",
        )
        optional = {"branch": self.identity.branch, "title": self.label.current}
        return {**hello, **{key: value for key, value in optional.items() if value}}

    # Claude Code -> Discord

    async def on_hook_call(self, arguments: Json, meta: Json) -> str:
        if MODEL_CALL in meta:
            return "Ignored: this tool is internal to the dui plugin's hooks."
        # A field the hook input lacks may arrive as its unsubstituted placeholder.
        fields = {key: value for key, value in arguments.items() if isinstance(value, str) and not value.startswith("${")}
        if fields.get("event") == "PermissionRequest":
            return await self.on_dialog(fields)
        async with self.hook_lock:
            await self.on_hook(fields)
        # Hook output text becomes context for the model, so always return none.
        return ""

    async def on_dialog(self, fields: dict[str, str]) -> str:
        """Wait while a terminal dialog is open; return Discord's decision, or nothing to leave it to the terminal."""
        async with self.hook_lock:
            await self.flush_pending_input()
            conversation = fields.get("session_id")
            if conversation and conversation != self.conversation_id:
                return ""
            tool_name, tool_input = fields.get("tool_name", ""), json_object(fields.get("tool_input"))
            path = fields.get("transcript_path", "")
            key = uuid.uuid4().hex
            dialog = Dialog(
                tool_name=tool_name,
                tool_input=tool_input or {},
                tool_use_id=self.tool_use_id(tool_name, tool_input),
                transcript_path=path,
                offset=await self.transcript.size(path),
                answer=asyncio.get_running_loop().create_future(),
            )
            self.dialogs[key] = dialog
            prompt = None if tool_input is None else self.dialog_prompt(key, dialog, fields)
            if prompt is not None and self.shows(prompt):
                self.prompts[key] = prompt
                self.enqueue(prompt)
            else:
                self.wait_locally(tool_name)
        try:
            decision = await dialog.answer
        except asyncio.CancelledError:
            # The hook timed out with the dialog still open in the terminal.
            if (prompt := self.prompts.pop(key, None)) is not None:
                self.retire(prompt)
                self.wait_locally(tool_name)
            raise
        return "" if decision is None else hook_decision(decision)

    def tool_use_id(self, tool_name: str, tool_input: Json | None) -> str | None:
        """The oldest unclaimed matching call, so identical parallel calls each keep their own ID."""
        for call in self.tool_calls:
            if call[:2] == (tool_name, tool_input):
                self.tool_calls.remove(call)
                return call[2]
        return None

    def dialog_prompt(self, key: str, dialog: Dialog, fields: dict[str, str]) -> Json | None:
        """The Discord prompt for a dialog Discord can show whole, or None to keep it in the terminal."""
        tool_input, turn_id = dialog.tool_input, fields.get("prompt_id", "")
        if dialog.tool_name == QUESTION_TOOL:
            questions = question_cards(tool_input.get("questions"))
            if questions is None:
                return None
            return self.event("request_user_input", call_id=key, turn_id=turn_id, questions=questions)
        command = tool_input.get("command")
        if dialog.tool_name == "Bash" and set(tool_input) <= COMMAND_FIELDS and isinstance(command, str):
            if not command_text_displayable(command):
                return None
            description = tool_input.get("description")
            return self.event(
                "approval_request",
                approval_id=key,
                call_id=dialog.tool_use_id or key,
                turn_id=turn_id,
                command=command_argv(command),
                command_text=command,
                cwd=fields.get("cwd") or self.identity.cwd,
                reason=description if isinstance(description, str) else None,
            )
        text = json.dumps({"tool": dialog.tool_name, "input": tool_input}, indent=2, ensure_ascii=False)
        if not approval_content_displayable("tool", text):
            return None
        return self.event(
            "approval_request",
            approval_id=key,
            call_id=dialog.tool_use_id or key,
            turn_id=turn_id,
            approval_kind="tool",
            content_text=text,
        )

    def shows(self, prompt: Json) -> bool:
        """Whether the connected Discord Blue shows this prompt as sent; until one connects, assume it does."""
        if self.server_features is None:
            return True
        if prompt["type"] == "request_user_input":
            # An older server shows a multi-select question as a single choice.
            return "multi_select" in self.server_features or not any(q["multiSelect"] for q in prompt["questions"])
        return ("command_text" if prompt.get("command_text") is not None else "tool_content") in self.server_features

    def on_server_features(self) -> None:
        for key, prompt in list(self.prompts.items()):
            if not self.shows(prompt):
                del self.prompts[key]
                self.wait_locally(self.dialogs[key].tool_name)

    def wait_locally(self, tool_name: str) -> None:
        message = waiting_message(tool_name)
        # Discord Blue retires open question cards on any status; an open card already shows the session waiting.
        if not self.prompts:
            self.publish("status_changed", message=message)
        # The thread also gets it as a message, since a status only changes the reaction.
        self.publish("notice", message=message)

    def retire(self, prompt: Json) -> None:
        """Close a Discord card that was not answered there."""
        if prompt["type"] == "approval_request":
            self.publish("approval_resolved", approval_id=prompt["approval_id"])
        else:
            self.publish("request_user_input_resolved", call_id=prompt["call_id"], turn_id=prompt["turn_id"])

    def close_dialog(self, key: str) -> None:
        dialog = self.dialogs.pop(key)
        if not dialog.answer.done():
            dialog.answer.set_result(None)
        if (prompt := self.prompts.pop(key, None)) is not None:
            self.retire(prompt)

    async def on_hook(self, fields: dict[str, str]) -> None:
        await self.flush_pending_input()
        event = fields.get("event")
        launch_brief = (
            self.launch_brief_pending
            and event == "UserPromptSubmit"
            and matches_launch_prompt(self.identity.launch_prompt, fields.get("prompt"))
        )
        self.launch_brief_pending = False
        conversation = fields.get("session_id") or None
        if event in {"SessionEnd", "UserPromptSubmit", "Notification"} or (conversation and conversation != self.conversation_id):
            # Claude waiting at its prompt or taking a new one means the last answer stood; it posts before what follows.
            await self.settle_turns()
        if event in {"SessionEnd", "UserPromptSubmit", "Notification", "Stop", "StopFailure"} or (
            conversation and conversation != self.conversation_id
        ):
            # No dialog outlives its turn.
            for key in list(self.dialogs):
                self.close_dialog(key)
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
        if event == "UserPromptSubmit":
            self.publish("status_changed", message="Turn started")
        elif event == "PreToolUse" and (tool_use_id := fields.get("tool_use_id")):
            self.tool_calls.append((fields.get("tool_name", ""), json_object(fields.get("tool_input")), tool_use_id))
        elif event in {"PostToolUse", "PostToolUseFailure"} and (tool_use_id := fields.get("tool_use_id")):
            # The terminal answered this tool call's dialog.
            answered = [key for key, dialog in self.dialogs.items() if dialog.tool_use_id == tool_use_id]
            for key in answered:
                self.close_dialog(key)
            if answered and not self.dialogs:
                self.publish("status_changed", message=CONTINUING)
        if event == "UserPromptSubmit":
            prompt = fields.get("prompt", "")
            echo = prompt.lstrip().startswith("<channel") and any(f'command_id="{c}"' in prompt for c in self.injected)
            # Claude Code also fires this hook for prompts it injects itself, such as task notifications.
            typed = None if echo else typed_prompt(prompt)
            titles = await self.retitle(fields, prompt=None if fields.get("prompt_id") else typed)
            if typed is not None and typed.strip() and fields.get("prompt_id") not in titles.scheduled_prompt_ids:
                if launch_brief and titles.before_first_prompt:
                    await self.retitle(fields, prompt=typed)
                    self.publish("user_message", message=clip(typed), input_origin=LAUNCH_BRIEF_ORIGIN)
                elif fields.get("prompt_id"):
                    self.pending_input = (fields, typed)
                else:
                    self.publish("user_message", message=clip(typed))
        elif event == "Stop":
            # Another plugin's Stop hook can reject this answer and send Claude back to rewrite it. Its verdict is
            # written only after every Stop hook returns, this one included, so it is read afterwards, in Stop order.
            offset = await self.transcript.size(fields.get("transcript_path", ""))
            self.pending_stop = asyncio.create_task(
                self.finish_turn(fields, offset, self.pending_stop), name=f"claude-stop-{self.session_id}"
            )
            # Claude Code writes its own title for the session as it answers.
            await self.retitle(fields)
        elif event == "StopFailure":
            self.publish("error", message="Claude Code ended the turn with an error; check the terminal.")
        elif event == "SessionStart":
            await self.retitle(fields)

    async def finish_turn(self, fields: dict[str, str], offset: int | None, previous: asyncio.Task[None] | None) -> None:
        if previous is not None:
            await asyncio.wait({previous})
        path = fields.get("transcript_path", "")
        blocked = None if offset is None else await self.transcript.stop_blocked(path, offset)
        loop = asyncio.get_running_loop()
        started = loop.time()
        while offset is not None and blocked is None and not self.settle.is_set():
            waited = loop.time() - started
            if waited >= STOP_VERDICT_SECONDS:
                break  # Unknown: post it rather than lose a final answer.
            # Most verdicts land within moments; a slow hook is then checked once a second.
            poll = STOP_VERDICT_POLL_SECONDS if waited < STOP_VERDICT_FAST_SECONDS else STOP_VERDICT_SLOW_POLL_SECONDS
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.settle.wait(), poll)
            blocked = await self.transcript.stop_blocked(path, offset)
        if not blocked:
            self.publish(
                "turn_complete", message=TURN_DONE, assistant_message=clip(fields.get("last_assistant_message", "")) or None
            )

    async def settle_turns(self) -> None:
        """Post any answer still awaiting its verdict now, before a new turn, conversation or session end follows it."""
        if self.pending_stop is None or self.pending_stop.done():
            return
        self.settle.set()
        try:
            await asyncio.wait({self.pending_stop})
        finally:
            self.settle.clear()

    async def flush_pending_input(self) -> None:
        pending, self.pending_input = self.pending_input, None
        if pending is None:
            return
        fields, typed = pending
        titles = await self.retitle(fields, prompt=typed)
        if fields["prompt_id"] not in titles.scheduled_prompt_ids:
            self.publish("user_message", message=clip(typed))

    async def send(self, websocket: aiohttp.ClientWebSocketResponse, message: Json) -> None:
        await super().send(websocket, message)
        if self.ending is not None and message is self.ending[0]:
            self.ending[1].set()

    async def end(self) -> None:
        try:
            async with asyncio.timeout(SESSION_END_TIMEOUT_SECONDS):
                await self.flush_pending_input()
                await self.settle_turns()
                if self.websocket is not None and not self.websocket.closed and not self.stopped.is_set():
                    terminal = self.event("session_end")
                    sent = asyncio.Event()
                    self.ending = (terminal, sent)
                    self.enqueue(terminal)
                    await sent.wait()
        except TimeoutError:
            logger.warning("Claude session ended before queued input and closure finished sending")
        finally:
            await self.stop()

    async def switch_conversation(self, conversation: str | None) -> None:
        """Start a new epoch so Discord controls meant for the previous conversation are rejected."""
        self.epoch = uuid.uuid4().hex
        self.commands.clear()
        self.prompts.clear()
        self.conversation_id, self.label = conversation, SessionLabel()
        if self.websocket is not None:
            # Reconnect so Discord Blue binds the thread to the new epoch; nothing more goes out on this socket.
            await self.websocket.close()
        # Mirrored events still queued belong in the thread; prompts for the old conversation do not.
        self.outbox = deque({**event, "session_epoch": self.epoch} for event in self.outbox if event["type"] not in PROMPT_EVENTS)
        ended = "The Claude Code conversation in this thread ended (/clear or /resume)"
        self.publish("notice", message=f"{ended}; earlier replies from Discord are no longer accepted.")

    async def retitle(self, fields: dict[str, str], prompt: str | None = None) -> Titles:
        """Name first (hook session_title, else the transcript's custom title), then Claude's aiTitle, then the prompt."""
        titles = await self.transcript.read(fields.get("transcript_path", ""))
        if fields.get("prompt_id") in titles.scheduled_prompt_ids:
            prompt = None
        name = fields.get("session_title") or titles.custom
        if (title := self.label.update(name=name, auto=titles.ai, prompt=prompt)) is not None:
            self.publish("title_changed", title=title)
        return titles

    # Discord -> Claude Code

    async def inject(self, command_id: str, text: str) -> None:
        self.injected.append(command_id)
        await self.notify(CHANNEL, {"content": text, "meta": {"command_id": command_id}})

    async def run_command(self, message: Json) -> object:
        kind = message.get("kind")
        if kind == "reply":
            text = message.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > REPLY_LIMIT:
                raise Rejected(f"Replies must contain 1 to {REPLY_LIMIT} characters.")
            await self.inject(str(message["command_id"]), text)
        elif kind == "status_request":
            self.enqueue(self.status_snapshot())
        elif kind == "request_user_input_response":
            key = str(message.get("call_id"))
            prompt = self.prompts.get(key)
            if prompt is None or prompt["type"] != "request_user_input" or prompt["turn_id"] != message.get("turn_id"):
                raise Rejected("This prompt is no longer pending.")
            dialog = self.dialogs[key]
            if await self.answered_in_terminal(key, dialog):
                raise Rejected(ANSWERED_IN_TERMINAL)
            if key not in self.prompts:
                raise Rejected("This prompt is no longer pending.")
            answers = question_answers(dialog.tool_input["questions"], message.get("response"))
            if answers is None:
                self.answer(key, {"behavior": "deny", "message": "The user dismissed the question in Discord."})
            else:
                self.answer(key, {"behavior": "allow", "updatedInput": {**dialog.tool_input, "answers": answers}})
        else:
            raise Rejected("Claude Code channels cannot do this; use the Claude Code terminal.")
        return None

    async def approval_decision(self, message: Json) -> Json:
        key = str(message.get("approval_id") or "")
        reject = self.event("approval_decision_reject", approval_id=key)
        if not self.is_current(message):
            return {**reject, "reason": "Stale session; the decision was not sent."}
        prompt, decision = self.prompts.get(key), message.get("decision")
        if prompt is None or prompt["type"] != "approval_request" or decision not in {"approved", "denied"}:
            return {**reject, "reason": "This approval is no longer pending."}
        dialog = self.dialogs[key]
        if await self.answered_in_terminal(key, dialog):
            return {**reject, "reason": ANSWERED_IN_TERMINAL}
        if key not in self.prompts:
            # Answered or closed while the transcript was read.
            return {**reject, "reason": "This approval is no longer pending."}
        if decision == "approved":
            # Exactly the input Discord showed, whatever Claude Code would otherwise run.
            self.answer(key, {"behavior": "allow", "updatedInput": dialog.tool_input})
        else:
            self.answer(key, {"behavior": "deny", "message": "The user denied this in Discord."})
        return self.event("approval_decision_ack", approval_id=key)

    async def answered_in_terminal(self, key: str, dialog: Dialog) -> bool:
        """Whether the call already has a result, as after a rejection in the terminal; its card is then closed."""
        if dialog.tool_use_id is None or dialog.offset is None:
            return False
        if not await self.transcript.tool_answered(dialog.transcript_path, dialog.offset, dialog.tool_use_id):
            return False
        if key in self.dialogs:
            self.close_dialog(key)
        return True

    def answer(self, key: str, decision: Json) -> None:
        """Hand Discord's decision to the waiting hook call; the card already shows who answered."""
        del self.prompts[key]
        dialog = self.dialogs.pop(key)
        if not dialog.answer.done():
            dialog.answer.set_result(decision)
        if not self.dialogs:
            self.publish("status_changed", message=CONTINUING)

    def failure_reason(self, exc: Exception) -> str:
        return LOST_CLAUDE
