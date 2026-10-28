"""How a session's status, prompts, answers and title changes show in its thread."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

import discord

from discord_blue.doodads.agent_session.chunks import DISCORD_MESSAGE_LIMIT
from discord_blue.doodads.agent_session.controls import (
    REACTION_COMPACTING,
    REACTION_FINISHED,
    REACTION_IN_PROGRESS,
    REACTION_REJECTED,
    SessionControls,
)
from discord_blue.doodads.agent_session.formatting import filter_injected_tags, format_user_message
from discord_blue.doodads.agent_session.interactions import SessionInteractions
from discord_blue.doodads.agent_session.messages import send_agent_session_message, send_assistant_message
from discord_blue.doodads.agent_session.protocol import LAUNCH_BRIEF_ORIGIN, SessionStatus, UserMessage
from discord_blue.doodads.agent_session.sessions import AgentSession, AgentSessionRegistry

if TYPE_CHECKING:
    from discord_blue.doodads.agent_session.director_questions import QuestionCards

logger = logging.getLogger(__name__)


class StatusHooks(Protocol):
    """What status routing needs from the bridge."""

    questions: QuestionCards
    sessions: AgentSessionRegistry
    controls: SessionControls
    interactions: SessionInteractions

    def thread_channel(self, thread_id: int) -> object | None: ...

    def request_thread_name(self, session: AgentSession) -> None: ...

    async def post_thread_notice(self, thread_id: int, text: str) -> None: ...


class SessionStatusUpdates:
    def __init__(self, bridge: StatusHooks) -> None:
        self.bridge = bridge

    async def handle_session_status(self, message_type: str, status: SessionStatus) -> None:
        session = self.bridge.sessions.get(status.session_id)
        if session is None or session.thread_id is None:
            logger.warning("Agent session status for unknown session: %s", status.session_id)
            return
        if status.session_epoch != session.session_epoch:
            logger.warning("Agent session status for stale session epoch: %s", status.session_id)
            return
        session.status_revision += 1
        session.last_status_message = status.message
        if message_type == "turn_complete":
            session.display_state = "done"
        elif message_type == "error":
            session.display_state = "failed"
        elif "unconfirmed" in (status.message or "").lower():
            session.display_state = "unknown"
        elif "waiting" in (status.message or "").lower() or (status.message or "").lower() == "turn aborted":
            session.display_state = "waiting"
        else:
            session.display_state = "working"

        if message_type == "status_changed":
            status_message = (status.message or "").lower()
            if status_message == "turn aborted":
                await self.bridge.controls.post_session_controls(session)
                return
            elif "compact" in status_message:
                reaction = REACTION_COMPACTING
            else:
                reaction = REACTION_IN_PROGRESS
            await self.bridge.interactions.clear_pending_user_inputs(
                session,
                "Agent session is no longer waiting on this prompt.",
            )
            await self.bridge.controls.update_session_status_reaction(session, reaction)
            return

        if message_type == "error":
            await self.bridge.interactions.clear_pending_user_inputs(
                session,
                "Agent session stopped waiting on this prompt.",
            )
            await self.bridge.controls.update_session_status_reaction(session, REACTION_REJECTED)
            try:
                await self.bridge.post_thread_notice(
                    session.thread_id, status.message or "The agent reported an error. Check the terminal."
                )
            except discord.DiscordException:
                logger.warning("Unable to post Agent session error in thread %s; failure remains on its card", session.thread_id)
            return

        if message_type == "turn_complete" and status.assistant_message:
            await self.post_assistant_message(session.thread_id, status.assistant_message)
        if message_type == "turn_complete":
            await self.bridge.controls.clear_rejected_command_reactions(session)
            await self.bridge.controls.update_active_command_reaction(session, REACTION_FINISHED, clear=True)
            await self.bridge.interactions.clear_pending_user_inputs(
                session,
                "Agent session is no longer waiting on this prompt.",
            )
            await self.bridge.controls.post_session_controls(session)

    async def handle_user_message(self, user_message: UserMessage) -> None:
        session = self.bridge.sessions.get(user_message.session_id)
        if session is None or session.thread_id is None:
            logger.warning("Agent session user message for unknown session: %s", user_message.session_id)
            return
        if user_message.session_epoch != session.session_epoch:
            logger.warning("Agent session user message for stale session epoch: %s", user_message.session_id)
            return
        hide_brief = session.hello.spawned and user_message.input_origin == LAUNCH_BRIEF_ORIGIN
        message = filter_injected_tags(user_message.message)
        if not message:
            return
        channel = self.bridge.thread_channel(session.thread_id)
        if not isinstance(channel, discord.Thread):
            return

        if not hide_brief:
            await send_agent_session_message(channel, format_user_message(message)[:DISCORD_MESSAGE_LIMIT])
        session.status_revision += 1
        session.display_state = "working"
        session.last_status_message = (
            "Working on the automated launch brief. Full input remains in the native terminal."
            if hide_brief
            else "Working on your request."
        )
        session.pending_control_confirmation = None
        await self.bridge.controls.spawn_session_controls(
            session,
            reaction=REACTION_IN_PROGRESS,
            interruptions_enabled=True,
        )

    async def post_assistant_message(self, thread_id: int, text: str) -> None:
        channel = self.bridge.thread_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            return
        session = self.bridge.sessions.get_by_thread(thread_id)
        if session is not None:
            await self.bridge.questions.send_answer(session, channel, text)
        else:
            await send_assistant_message(channel, text)

    async def handle_title_changed(self, session: AgentSession, title: object) -> None:
        """Rename the session thread for a title the client learned after hello, such as its latest prompt."""
        if not isinstance(title, str) or not title.strip() or session.thread_id is None:
            return
        session.hello.title = title.strip()
        self.bridge.request_thread_name(session)
