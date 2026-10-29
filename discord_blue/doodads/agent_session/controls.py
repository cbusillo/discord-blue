"""What a session shows while it works, and the commands an operator sends a session from Discord."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Literal, Protocol

import discord

if TYPE_CHECKING:
    from discord_blue.doodads.agent_session.director_questions import QuestionCards
    from discord_blue.doodads.agent_session.handover import SessionContinuation

from discord_blue.doodads.agent_session.protocol import RemoteCommand, payload_string
from discord_blue.doodads.agent_session.sessions import (
    AgentSession,
    AgentSessionRegistry,
    PendingRemoteCommand,
    PendingRemoteUserInput,
    RejectedCommandMessage,
)
from discord_blue.doodads.agent_session.store import QuestionState, SessionStore
from discord_blue.doodads.agent_session.thread_worker import ThreadWorkers
from discord_blue.doodads.agent_session.threads import session_thread_name
from discord_blue.plugs.discord_plug import BlueBot

logger = logging.getLogger(__name__)
DELIVERY_UNCONFIRMED = "Agent session connection failed; action delivery could not be confirmed. Check the native TUI."
REPLY_BEFORE_RECONNECT = (
    "This reply was written before the agent session reconnected (for example after `/clear` or `/resume`), "
    "so it was not delivered. Send it again if it still applies."
)
CONTINUE_AUTONOMOUSLY_DELIVERED = "Asked the agent session to go ahead until it needs you."
PAUSE_CURRENT_TURN_DELIVERED = "Asked the agent session to pause what it is doing now."


@dataclasses.dataclass(frozen=True, slots=True)
class SessionControl:
    kind: Literal["continue_autonomously", "pause_current_turn", "new_session", "end_session"]
    slash_command: str
    operator_action: str
    offline_reply: str
    reject_notice: str
    delivered: str
    pending_status: str | None = None


GO_AHEAD_CONTROL = SessionControl(
    kind="continue_autonomously",
    slash_command="go-ahead",
    operator_action="ask a session to continue",
    offline_reply="Agent session is offline; go-ahead was not delivered.",
    reject_notice="Agent session could not go ahead",
    delivered=CONTINUE_AUTONOMOUSLY_DELIVERED,
    pending_status="Your request is queued.",
)
PAUSE_CONTROL = SessionControl(
    kind="pause_current_turn",
    slash_command="pause",
    operator_action="pause a turn",
    offline_reply="Agent session is offline; pause was not delivered.",
    reject_notice="Agent session could not pause the current turn",
    delivered=PAUSE_CURRENT_TURN_DELIVERED,
    pending_status="Pause requested; waiting for the native session.",
)
NEW_SESSION_CONTROL = SessionControl(
    kind="new_session",
    slash_command="new",
    operator_action="start a new session",
    offline_reply="Agent session is offline; new session was not started.",
    reject_notice="Agent session could not start a new session",
    delivered="Asked the agent session to start a new session in this folder.",
)
END_SESSION_CONTROL = SessionControl(
    kind="end_session",
    slash_command="end-session",
    operator_action="end a session",
    offline_reply="Agent session is already offline.",
    reject_notice="Agent session could not end the session",
    delivered="Asked the agent session to end this session.",
    pending_status="Session end requested; waiting for the native session.",
)
REACTION_QUEUED = "⏳"
REACTION_DELIVERED = "📬"
REACTION_IN_PROGRESS = "🔄"
REACTION_COMPACTING = "🧹"
REACTION_FINISHED = "✅"
REACTION_REJECTED = "❌"
# Discord shows a typing indicator for about ten seconds per request.
TYPING_REFRESH_SECONDS = 8
# How long an answer waits for a typing request already in flight, which would otherwise land after it.
TYPING_SETTLE_SECONDS = 1
STATUS_REACTIONS = {
    REACTION_QUEUED,
    REACTION_DELIVERED,
    REACTION_IN_PROGRESS,
    REACTION_COMPACTING,
    REACTION_FINISHED,
    REACTION_REJECTED,
}
REACTION_APPROVAL_APPROVE = "✅"
REACTION_APPROVAL_DENY = "✖️"
TRANSIENT_REACTIONS = STATUS_REACTIONS


async def defer_control(interaction: discord.Interaction, *, ephemeral: bool = False) -> bool:
    try:
        await interaction.response.defer(ephemeral=ephemeral)
    except discord.NotFound as exc:
        if exc.code != 10062:
            raise
        if isinstance(interaction.channel, (discord.Thread, discord.TextChannel)):
            await interaction.channel.send(
                "That control expired during the update. Please try again.", allowed_mentions=discord.AllowedMentions.none()
            )
        return False
    return True


@dataclasses.dataclass(slots=True)
class ReactionWrites:
    """The bot's reaction changes to one message: one request at a time, and the latest change supersedes the rest."""

    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)
    latest: int = 0


class ControlHooks(Protocol):
    """What the controls need from the bridge."""

    bot: BlueBot
    questions: QuestionCards
    sessions: AgentSessionRegistry
    store: SessionStore
    threads: ThreadWorkers
    handover: SessionContinuation

    def is_operator(self, user: discord.User | discord.Member) -> bool: ...

    async def post_thread_notice(self, thread_id: int, text: str) -> None: ...

    def thread_channel(self, thread_id: int) -> object | None: ...


class SessionControls:
    def __init__(self, bridge: ControlHooks) -> None:
        self.bridge = bridge
        # By message ID, while the bot is changing that message's reactions.
        self._reaction_writes: dict[int, ReactionWrites] = {}

    def dispatch_error(self, session: AgentSession, action: str) -> str | None:
        if self.bridge.sessions.get(session.session_id) is not session or session.websocket.closed:
            return "Agent session is offline; action was not delivered."
        if not session.hello.supports(action):
            label = {
                "reply": "replies",
                "status_request": "status requests",
                "pause_current_turn": "pausing turns",
                "end_session": "ending sessions",
                "new_session": "starting new sessions",
                "continue_autonomously": "autonomous continuation",
                "request_user_input_response": "answering prompts",
                "approval_decision": "approval decisions",
            }.get(action, "this action")
            return f"This client does not support {label} from Discord. Use the native TUI."
        return None

    async def dispatch_command(
        self,
        session: AgentSession,
        command: RemoteCommand,
        pending: PendingRemoteCommand,
        *,
        input_pending: PendingRemoteUserInput | None = None,
        before_send: Callable[[], Awaitable[None]] | None = None,
    ) -> str | None:
        if error := self.dispatch_error(session, command.kind):
            return error
        if input_pending is not None:
            input_pending.submitted = True
        pending.status_revision = session.status_revision
        pending.previous_display_state = session.display_state
        pending.previous_status_message = session.last_status_message
        session.pending_commands[command.command_id] = pending
        delivered = False
        try:
            if before_send is not None:
                try:
                    await before_send()
                except (OSError, discord.DiscordException):
                    return "Discord could not prepare the reply controls; reply was not sent."
            if error := self.dispatch_error(session, command.kind):
                return error
            try:
                await session.websocket.send_json(command.to_message())
            except (OSError, RuntimeError):
                return DELIVERY_UNCONFIRMED
            delivered = True
            return None
        finally:
            if not delivered:
                session.pending_commands.pop(command.command_id, None)
                if input_pending is not None:
                    input_pending.submitted = False

    async def send_thread_reply(self, message: discord.Message) -> bool:
        text = message.content.strip()
        if not text or text.startswith("!"):
            return False
        saved_thread = self.bridge.handover.has_thread(message.channel.id)
        session = await self.bridge.handover.wait_for_thread(message.channel.id)
        if session is None:
            for session_id, stored in tuple(self.bridge.store.records.items()):
                if stored.thread_id == message.channel.id:
                    if await self.bridge.questions.decisions(session_id, message.content, url=message.jump_url):
                        await message.reply(
                            "Decision recorded. This session is not connected, so the reply was not delivered.", mention_author=False
                        )
                        return True
            if saved_thread:
                await message.reply("Agent session is offline; reply was not delivered.", mention_author=False)
                return True
            return False
        # MESSAGE_CREATE can arrive before the reopened thread returns to discord.py's cache.
        # Its PartialMessageable still identifies the thread already bound by our acknowledged hello.
        channel = self.bridge.thread_channel(message.channel.id)
        if not isinstance(channel, discord.Thread):
            return False
        thread: discord.Thread = channel
        if message.created_at < session.attached_at:
            # The client reconnected since (for Claude Code, after /clear or /resume); never deliver it to the new epoch.
            await message.reply(REPLY_BEFORE_RECONNECT, mention_author=False)
            return True
        await self.bridge.questions.decisions(session.session_id, text, url=message.jump_url)
        if session.websocket.closed:
            await message.reply("Agent session is offline; reply was not delivered.", mention_author=False)
            return True
        if not session.acknowledged:
            await message.reply("Agent session is reconnecting; send the reply again in a moment.", mention_author=False)
            return True

        # Answers already given on Question cards travel with this reply.
        held, keys = await self.bridge.questions.release_held(session)
        error = await self.send_reply(
            session, thread, message.author, f"{held}\n\n{text}" if held else text, message.id, question_keys=keys
        )
        if error is not None:
            outcome = QuestionState.UNCONFIRMED if error == DELIVERY_UNCONFIRMED else QuestionState.HELD
            await self.bridge.questions.settle(session.session_id, keys, outcome)
            await message.reply(error, mention_author=False)
        return True

    async def send_reply(
        self,
        session: AgentSession,
        thread: discord.Thread,
        user: discord.User | discord.Member,
        text: str,
        message_id: int,
        *,
        question_keys: tuple[str, ...] = (),
        react: bool = True,
    ) -> str | None:
        command = RemoteCommand(
            command_id=str(uuid.uuid4()),
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            kind="reply",
            text=text,
            issued_by=str(user.id),
        )
        pending = PendingRemoteCommand(
            thread_id=thread.id,
            message_id=message_id,
            kind="reply",
            question_keys=question_keys,
            react=react,
        )
        queued = False

        async def show_queued() -> None:
            nonlocal queued
            queued = True
            if react:
                await self.set_message_reaction(thread.id, message_id, REACTION_QUEUED)
            self.show_working(session, "Your request is queued.")

        delivered = False
        try:
            error = await self.dispatch_command(session, command, pending, before_send=show_queued)
            delivered = error is None
        finally:
            if queued and not delivered:
                if react:
                    await self.clear_message_transient_reactions(thread.id, message_id)
                self.restore_command_status(session, pending)
                self.show_activity(session)
        return error

    async def send_continue_autonomously(self, channel: object, user: discord.User | discord.Member) -> str:
        return await self.send_session_control(channel, user, GO_AHEAD_CONTROL)

    async def send_pause_current_turn(self, channel: object, user: discord.User | discord.Member) -> str:
        return await self.send_session_control(channel, user, PAUSE_CONTROL)

    async def send_new_session(self, channel: object, user: discord.User | discord.Member) -> str:
        return await self.send_session_control(channel, user, NEW_SESSION_CONTROL)

    async def send_end_session(self, channel: object, user: discord.User | discord.Member) -> str:
        return await self.send_session_control(channel, user, END_SESSION_CONTROL)

    async def send_session_control(
        self,
        channel: object,
        user: discord.User | discord.Member,
        control: SessionControl,
    ) -> str:
        if not isinstance(channel, discord.Thread):
            return f"Use `/code {control.slash_command}` inside an agent session thread."
        if not self.bridge.is_operator(user):
            return f"Only agent session operators can {control.operator_action}."

        session = self.bridge.sessions.get_by_thread(channel.id)
        if session is None:
            return "This thread is not attached to a live agent session."
        if session.websocket.closed:
            return control.offline_reply

        command = RemoteCommand(
            command_id=str(uuid.uuid4()),
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            kind=control.kind,
            issued_by=str(user.id),
        )
        pending = PendingRemoteCommand(
            thread_id=channel.id,
            message_id=None,
            kind=control.kind,
            reject_notice=control.reject_notice,
        )
        if error := await self.dispatch_command(session, command, pending):
            return error
        if control.pending_status is not None:
            self.show_working(session, control.pending_status)
        return control.delivered

    def session_status_summary(
        self,
        channel: object,
        user: discord.User | discord.Member,
    ) -> str:
        if not isinstance(channel, discord.Thread):
            return "Use `/code status` inside an agent session thread."
        if not self.bridge.is_operator(user):
            return "Only agent session operators can inspect session status."

        session = self.bridge.sessions.get_by_thread(channel.id)
        if session is None:
            for record in self.bridge.store.records.values():
                if record.thread_id == channel.id:
                    return f"Session disconnected\n{record.marker.removeprefix('Agent session connected').lstrip()}"
            return "This thread is not attached to a live agent session."

        title = session_thread_name(session.hello)
        state = "offline" if session.websocket.closed else "online"
        status = session.last_status_message or "No status update received yet."
        return "\n".join(
            [
                f"Agent session `{title}`",
                f"state: {state}",
                f"host: {session.hello.host_label}",
                f"cwd: `{session.hello.cwd}`",
                f"branch: `{session.hello.branch or 'unknown'}`",
                f"session: `{session.session_id}`",
                f"pid: `{session.hello.pid}`",
                f"status: {status}",
            ]
        )

    async def handle_command_ack(self, payload: dict[str, object]) -> None:
        command_context = self.command_context(payload)
        if command_context is None:
            return
        command_id, session = command_context
        command = session.pending_commands.get(command_id)
        if command is not None:
            session.active_command_id = command_id
            await self.bridge.questions.settle(session.session_id, command.question_keys, QuestionState.ANSWERED)
            await self.update_command_message_reaction(session, command, REACTION_DELIVERED)
            if command.kind == "reply" and command.status_revision == session.status_revision:
                session.last_status_message = "Request delivered to the native session."

    async def handle_command_reject(self, payload: dict[str, object]) -> None:
        command_context = self.command_context(payload)
        if command_context is None:
            return
        command_id, session = command_context
        command = session.pending_commands.pop(command_id, None)
        if session.active_command_id == command_id:
            session.active_command_id = None
        if command is not None:
            self.restore_command_status(session, command)
            await self.bridge.questions.settle(session.session_id, command.question_keys, QuestionState.NOT_DELIVERED)
            await self.update_command_message_reaction(session, command, REACTION_REJECTED)
            if command.message_id is not None and command.react:
                session.rejected_command_messages.append(
                    RejectedCommandMessage(
                        thread_id=command.thread_id,
                        message_id=command.message_id,
                    )
                )
            self.show_activity(session)
            if command.reject_notice is not None:
                reason = payload_string(payload, "reason", "command was rejected")
                await self.bridge.post_thread_notice(command.thread_id, f"{command.reject_notice}: {reason}")
            elif command.kind == "reply":
                reason = payload_string(payload, "reason", "reply was rejected")
                await self.bridge.post_thread_notice(command.thread_id, f"Reply not delivered: {reason}")

    @staticmethod
    def restore_command_status(session: AgentSession, command: PendingRemoteCommand) -> None:
        # A newer native event wins over an older command's delivery outcome.
        if command.status_revision != session.status_revision:
            return
        session.display_state = command.previous_display_state
        session.last_status_message = command.previous_status_message

    def command_context(self, payload: dict[str, object]) -> tuple[str, AgentSession] | None:
        command_id = payload_string(payload, "command_id")
        session_id = payload_string(payload, "session_id")
        if not command_id or not session_id:
            return None
        session = self.bridge.sessions.get(session_id)
        if session is None:
            return None
        return command_id, session

    async def update_command_message_reaction(
        self,
        session: AgentSession,
        command: PendingRemoteCommand,
        reaction: str,
    ) -> None:
        if command.input_prompt is not None:
            async with command.input_prompt.ui_lock:
                if command.input_prompt.retired:
                    return
                await self.set_message_reaction(command.thread_id, command.input_prompt.message_id, reaction)
            return
        if command.message_id is None or not command.react:
            return
        if command.kind == "reply" and reaction != REACTION_REJECTED:
            await self.clear_message_transient_reactions(command.thread_id, command.message_id)
            return
        await self.set_message_reaction(command.thread_id, command.message_id, reaction)

    async def update_active_command_reaction(
        self,
        session: AgentSession,
        reaction: str,
        *,
        clear: bool = False,
    ) -> None:
        command_id = session.active_command_id
        if command_id is None:
            return
        command = session.pending_commands.get(command_id)
        if command is None:
            return
        await self.update_command_message_reaction(session, command, reaction)
        if clear:
            session.pending_commands.pop(command_id, None)
            session.active_command_id = None

    async def clear_rejected_command_reactions(self, session: AgentSession) -> None:
        rejected_messages = session.rejected_command_messages
        session.rejected_command_messages = []
        for rejected_message in rejected_messages:
            await self.clear_message_transient_reactions(rejected_message.thread_id, rejected_message.message_id)

    async def update_session_status_reaction(
        self,
        session: AgentSession,
        reaction: str,
    ) -> None:
        await self.update_active_command_reaction(session, reaction)
        self.show_activity(session)

    def show_working(self, session: AgentSession, detail: str) -> None:
        session.display_state = "working"
        session.last_status_message = detail
        self.show_activity(session)

    def working(self, session: AgentSession) -> bool:
        return (
            session.display_state == "working"
            and session.thread_id is not None
            and self.bridge.sessions.get(session.session_id) is session
        )

    def show_activity(self, session: AgentSession) -> None:
        """Keep Discord's typing indicator up in the thread exactly while the session works, from one task."""
        task = session.typing_task
        if task is not None and not task.done():
            session.typing_wake.set()
        elif self.working(session):
            task = session.typing_task = asyncio.create_task(
                self.keep_typing(session), name=f"agent-session-typing-{session.session_id}"
            )
            self.bridge.threads.background.add(task)
            task.add_done_callback(self.bridge.threads.request_done)

    async def keep_typing(self, session: AgentSession) -> None:
        # Stops between requests, never canceled during one: see ThreadWorkers.stop.
        while self.working(session):
            session.typing_wake.clear()
            channel = self.bridge.thread_channel(session.thread_id or 0)
            if isinstance(channel, discord.Thread):
                try:
                    await channel.typing()
                except discord.DiscordException:
                    logger.debug("Unable to show typing in Agent session thread %s", channel.id)
            with suppress(TimeoutError):
                await asyncio.wait_for(session.typing_wake.wait(), TYPING_REFRESH_SECONDS)

    @staticmethod
    async def settle_activity(session: AgentSession) -> None:
        """Let a typing request already in flight land first, so it cannot show typing again after a post."""
        task = session.typing_task
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=TYPING_SETTLE_SECONDS)

    async def set_message_reaction(self, thread_id: int, message_id: int, reaction: str) -> None:
        channel = self.bridge.thread_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            return
        try:
            message = await channel.fetch_message(message_id)
        except discord.DiscordException:
            logger.warning("Unable to fetch Agent session reply message %s", message_id)
            return

        bot_user = self.bridge.bot.user
        try:
            await message.add_reaction(reaction)
            if bot_user is not None:
                for existing in TRANSIENT_REACTIONS - {reaction}:
                    with suppress(discord.DiscordException):
                        await message.remove_reaction(existing, bot_user)
        except discord.DiscordException:
            logger.warning("Unable to update Agent session reply reaction %s", message_id)

    async def clear_message_transient_reactions(self, thread_id: int, message_id: int) -> None:
        channel = self.bridge.thread_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            return
        bot_user = self.bridge.bot.user
        if bot_user is None:
            return
        try:
            message = await channel.fetch_message(message_id)
        except discord.DiscordException:
            logger.warning("Unable to fetch Agent session reply message %s", message_id)
            return

        for existing in TRANSIENT_REACTIONS:
            with suppress(discord.DiscordException):
                await message.remove_reaction(existing, bot_user)

    async def clear_message_reactions(self, message: discord.Message) -> None:
        await self.write_message_reactions(message, [], clear=True)

    async def write_message_reactions(
        self,
        message: discord.Message,
        reactions: list[str],
        *,
        clear: bool = False,
        remove_user_reaction: tuple[str, discord.User | discord.Member] | None = None,
    ) -> None:
        """Change the bot's reactions on `message`, superseding any change still under way.

        The bot adds reactions one request at a time, and an operator can tap the first while the rest are still
        coming. The handler's change must be the last word: it waits for a request already in flight, which would
        otherwise land after it, and the superseded change sends nothing more.
        """
        writes = self._reaction_writes.setdefault(message.id, ReactionWrites())
        writes.latest += 1
        mine = writes.latest
        # (request, reaction to name if it fails); only a failed add is worth a warning.
        steps: list[tuple[Callable[[], Awaitable[None]], str | None]] = []
        if remove_user_reaction is not None:
            emoji, user = remove_user_reaction
            steps.append((lambda: message.remove_reaction(emoji, user), None))
        if clear:
            steps.append((message.clear_reactions, None))
        steps.extend((functools.partial(message.add_reaction, reaction), reaction) for reaction in reactions)
        try:
            for request, added in steps:
                async with writes.lock:
                    if writes.latest != mine:
                        return
                    try:
                        await request()
                    except discord.DiscordException:
                        if added is not None:
                            logger.warning("Unable to add Agent session reaction %s to %s", added, message.id)
        finally:
            if writes.latest == mine and self._reaction_writes.get(message.id) is writes:
                del self._reaction_writes[message.id]

    @staticmethod
    async def remove_message_reaction(
        thread: discord.Thread,
        message_id: int,
        reaction: str,
        user: discord.User | discord.Member,
    ) -> None:
        try:
            message = await thread.fetch_message(message_id)
            await message.remove_reaction(reaction, user)
        except discord.DiscordException:
            logger.warning("Unable to remove Agent session reaction %s from %s", reaction, message_id)
