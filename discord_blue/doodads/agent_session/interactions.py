"""Approval and question cards a session posts in its thread, and the operator's answers to them."""

from __future__ import annotations

import dataclasses
import logging
import shlex
import uuid
from collections.abc import Callable
from typing import Literal, Protocol, cast

import discord

from discord_blue.doodads.agent_session.cards import notice_card
from discord_blue.doodads.agent_session.chunks import DISCORD_MESSAGE_LIMIT
from discord_blue.doodads.agent_session.controls import (
    REACTION_APPROVAL_APPROVE,
    REACTION_APPROVAL_DENY,
    SessionControls,
    defer_control,
)
from discord_blue.doodads.agent_session.formatting import strip_assistant_markers
from discord_blue.doodads.agent_session.messages import (
    agent_session_allowed_mentions,
    edit_agent_session_message,
    message_uses_components_v2,
    send_agent_session_message,
)
from discord_blue.doodads.agent_session.protocol import (
    APPROVAL_COMMAND_DISPLAY_LIMIT,
    RemoteApprovalDecision,
    RemoteApprovalRequest,
    RemoteCommand,
    RemoteRequestUserInput,
    RequestUserInputQuestion,
    approval_content_displayable,
    command_text_displayable,
    format_content_approval,
    payload_string,
)
from discord_blue.doodads.agent_session.question_modal import AnswerTogetherButton, questionnaire_fits_modal
from discord_blue.doodads.agent_session.sessions import (
    AgentSession,
    AgentSessionRegistry,
    PendingRemoteApproval,
    PendingRemoteCommand,
    PendingRemoteUserInput,
)
from discord_blue.plugs.discord_plug import BlueBot

logger = logging.getLogger(__name__)

MAX_INPUT_QUESTIONS = 5
INPUT_ANSWER_PREVIEW_LIMIT = 180


def bind_prompt_controls(view: discord.ui.LayoutView, identity: str) -> None:
    for item in view.walk_children():
        if isinstance(item, (discord.ui.Button, discord.ui.Select)):
            question = getattr(item, "question", None)
            key = getattr(item, "decision", None) or (question.id if question is not None else "")
            item.custom_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{identity}:{type(item).__name__}:{key}").hex


class ApprovalButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self, interactions: SessionInteractions, approval: RemoteApprovalRequest, decision: Literal["approved", "denied"]
    ) -> None:
        super().__init__(
            label="Approve once" if decision == "approved" else "Deny",
            style=discord.ButtonStyle.success if decision == "approved" else discord.ButtonStyle.danger,
        )
        self.interactions = interactions
        self.approval = approval
        self.decision: Literal["approved", "denied"] = decision

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.interactions.handle_approval_interaction(
            cast(discord.Interaction[BlueBot], interaction),
            self.approval.session_id,
            self.approval.approval_id,
            self.decision,
            session_epoch=self.approval.session_epoch,
        )


class ApprovalView(discord.ui.LayoutView):
    def __init__(self, interactions: SessionInteractions, approval: RemoteApprovalRequest, content: str) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(strip_assistant_markers(content)),
                discord.ui.Separator(),
                discord.ui.ActionRow(
                    ApprovalButton(interactions, approval, "approved"), ApprovalButton(interactions, approval, "denied")
                ),
                accent_color=0xF0B232,
            )
        )
        bind_prompt_controls(self, f"{approval.session_id}:{approval.session_epoch}:{approval.approval_id}")


class RequestUserInputSelect(discord.ui.Select[discord.ui.LayoutView]):
    def __init__(
        self,
        parent_view: RequestUserInputView,
        question: RequestUserInputQuestion,
    ) -> None:
        options = [
            discord.SelectOption(
                label=f"{index + 1}. {option.label}"[:100],
                value=str(index),
                description=(option.description[:100] or None),
            )
            for index, option in enumerate(question.options)
        ]
        placeholder = (question.header or question.question or "Choose an answer")[:150]
        super().__init__(placeholder=placeholder, options=options, max_values=len(options) if question.multi_select else 1)
        self.parent_view = parent_view
        self.question = question

    async def callback(self, interaction: discord.Interaction) -> None:
        labels = [
            self.question.options[int(value)].label
            for value in self.values
            if value.isdecimal() and int(value) < len(self.question.options)
        ]
        if not labels:
            return
        await self.parent_view.edit_answer(interaction, self.question.id, labels if self.question.multi_select else labels[0])


class RequestUserInputAnswerModal(discord.ui.Modal):
    def __init__(
        self,
        parent_view: RequestUserInputView,
        question: RequestUserInputQuestion,
    ) -> None:
        title = (question.header or question.question or "Agent session input")[:45]
        super().__init__(title=title)
        self.parent_view = parent_view
        self.question = question
        self.answer = cast(
            discord.ui.TextInput[RequestUserInputAnswerModal],
            discord.ui.TextInput(
                label=(question.header or question.question or "Answer")[:45],
                placeholder=(question.question[:100] or None),
                style=discord.TextStyle.paragraph if not question.options else discord.TextStyle.short,
                max_length=1500,
            ),
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.parent_view.edit_answer(interaction, self.question.id, self.answer.value.strip())


class RequestUserInputAnswerButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        parent_view: RequestUserInputView,
        question: RequestUserInputQuestion,
    ) -> None:
        label = "Other answer" if question.options and not question.is_secret else "Write answer"
        super().__init__(
            label=label,
            emoji="✏️",
        )
        self.parent_view = parent_view
        self.question = question

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RequestUserInputAnswerModal(self.parent_view, self.question))


class RequestUserInputSubmitButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(self, parent_view: RequestUserInputView) -> None:
        super().__init__(label="Send answer", style=discord.ButtonStyle.primary, emoji="✅")
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.submit(cast(discord.Interaction[BlueBot], interaction))


class RequestUserInputCancelButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(self, parent_view: RequestUserInputView) -> None:
        super().__init__(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️")
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.cancel(cast(discord.Interaction[BlueBot], interaction))


class RequestUserInputView(discord.ui.LayoutView):
    def __init__(
        self,
        interactions: SessionInteractions,
        session_id: str,
        request: RemoteRequestUserInput,
    ) -> None:
        super().__init__(timeout=3600)
        self.interactions = interactions
        self.session_id = session_id
        self.request = request
        self.answers: dict[str, str] = {}
        # A multi-select question's choices; its entry in answers shows them joined.
        self.picks: dict[str, list[str]] = {}
        self.message_id: int | None = None

        self.question_texts: dict[str, discord.ui.TextDisplay] = {}
        children: list[discord.ui.Item[discord.ui.LayoutView]] = [
            discord.ui.TextDisplay("### ⏸ Waiting on you\nChoose an answer for each question, then press **Send answer**."),
        ]
        for question in request.questions:
            children.append(discord.ui.Separator())
            text: discord.ui.TextDisplay[discord.ui.LayoutView] = discord.ui.TextDisplay(self.question_text(question))
            self.question_texts[question.id] = text
            children.append(text)
            if question.options:
                children.append(discord.ui.ActionRow(RequestUserInputSelect(self, question)))
            if not question.options or question.is_other:
                children.append(discord.ui.ActionRow(RequestUserInputAnswerButton(self, question)))
        children.append(discord.ui.Separator())
        if questionnaire_fits_modal(request):
            children.append(discord.ui.ActionRow(AnswerTogetherButton(self)))
        children.append(discord.ui.ActionRow(RequestUserInputSubmitButton(self), RequestUserInputCancelButton(self)))
        self.add_item(discord.ui.Container(*children, accent_color=0xF0B232))
        bind_prompt_controls(self, f"{request.session_id}:{request.session_epoch}:{request.turn_id}:{request.call_id}")

    def question_text(self, question: RequestUserInputQuestion) -> str:
        answer = self.answers.get(question.id, "")
        header = discord.utils.escape_markdown((question.header or question.id or "Question")[:80])
        text = f"{'✅' if answer else '⬜'} **{header}**\n{question.question}"
        if question.is_secret or any(len(o.label) > 96 or len(o.description) > 100 for o in question.options):
            for index, option in enumerate(question.options):
                text += f"\n{index + 1}. {option.label}"
                if option.description:
                    text += f": {option.description}"
        if answer:
            shown = "[hidden]" if question.is_secret else discord.utils.escape_markdown(answer[:INPUT_ANSWER_PREVIEW_LIMIT])
            text += f"\nSelected: {shown}"
        elif question.is_secret:
            text += "\nThis card hides your answer. Do not enter secrets in Discord."
        return strip_assistant_markers(text)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return (
            await self.interactions.user_input_context(
                cast(discord.Interaction[BlueBot], interaction),
                self.session_id,
                self.request.session_epoch,
                self.request.call_id,
                self.request.turn_id,
                self.message_id,
            )
            is not None
        )

    async def edit_answer(self, interaction: discord.Interaction, question_id: str, answer: str | list[str]) -> None:
        context = await self.interactions.user_input_context(
            cast(discord.Interaction[BlueBot], interaction),
            self.session_id,
            self.request.session_epoch,
            self.request.call_id,
            self.request.turn_id,
            self.message_id,
        )
        if context is None:
            return
        _, pending = context
        async with pending.ui_lock:
            if not await self.interaction_check(interaction):
                return
            self.set_answer(question_id, answer)
            await interaction.response.edit_message(
                content=None,
                view=self,
                allowed_mentions=agent_session_allowed_mentions(),
            )

    def set_answer(self, question_id: str, answer: str | list[str]) -> None:
        if isinstance(answer, list):
            self.picks[question_id] = answer
            answer = ", ".join(answer)
        self.answers[question_id] = answer
        for question in self.request.questions:
            if question.id == question_id:
                self.question_texts[question_id].content = self.question_text(question)
        for item in self.walk_children():
            if isinstance(item, RequestUserInputSelect) and item.question.id == question_id:
                picks = self.picks.get(question_id) if item.question.multi_select else None
                selected = False
                for index, option in enumerate(item.options):
                    label = item.question.options[index].label
                    if picks is not None:
                        option.default = label in picks
                        continue
                    option.default = not item.question.is_secret and not selected and label == answer
                    selected = selected or option.default

    def response_payload(self) -> dict[str, object]:
        return {
            "answers": {
                question.id: {"answers": self.picks.get(question.id) or [self.answers.get(question.id, "")]}
                for question in self.request.questions
            }
        }

    async def submit(self, interaction: discord.Interaction[BlueBot]) -> None:
        missing = [
            question.header or question.id or "Question"
            for question in self.request.questions
            if not self.answers.get(question.id, "").strip()
        ]
        if missing:
            await interaction.response.send_message(
                "Please answer before submitting: " + ", ".join(missing),
                ephemeral=True,
            )
            return
        await self.interactions.handle_request_user_input_interaction(
            interaction,
            self.session_id,
            self.request.call_id,
            self.request.turn_id,
            self.response_payload(),
            session_epoch=self.request.session_epoch,
            message_id=self.message_id,
        )

    async def cancel(self, interaction: discord.Interaction[BlueBot]) -> None:
        await self.interactions.handle_request_user_input_interaction(
            interaction,
            self.session_id,
            self.request.call_id,
            self.request.turn_id,
            {"answers": {}},
            cancelled=True,
            session_epoch=self.request.session_epoch,
            message_id=self.message_id,
        )


class InteractionHooks(Protocol):
    """What approvals and questions need from the bridge."""

    sessions: AgentSessionRegistry
    controls: SessionControls

    def bot_user_id(self) -> int | None: ...

    def is_operator(self, user: discord.User | discord.Member) -> bool: ...

    async def post_thread_notice(self, thread_id: int, text: str) -> None: ...

    def thread_channel(self, thread_id: int) -> object | None: ...


class SessionInteractions:
    def __init__(self, bridge: InteractionHooks) -> None:
        self.bridge = bridge

    async def dispatch_approval(
        self,
        session: AgentSession,
        pending: PendingRemoteApproval,
        approval_id: str,
        decision: Literal["approved", "denied"],
        user_id: int,
    ) -> str | None:
        if error := self.bridge.controls.dispatch_error(session, "approval_decision"):
            return error
        if pending.retired or session.pending_approvals.get(approval_id) is not pending:
            return "This approval is no longer active."
        if pending.decision is not None:
            return "This approval has already been answered."
        pending.decision, pending.decided_by = decision, user_id
        delivered = False
        try:
            await session.websocket.send_json(
                RemoteApprovalDecision(
                    approval_id=approval_id,
                    session_id=session.session_id,
                    session_epoch=session.session_epoch,
                    decision=decision,
                ).to_message()
            )
            delivered = True
        except (OSError, RuntimeError):
            return "Agent session connection failed; approval delivery could not be confirmed. Check the native TUI."
        finally:
            if not delivered:
                pending.decision, pending.decided_by = None, None
        return None

    async def prompt_message(self, channel: discord.Thread, view: discord.ui.LayoutView, message_id: int | None) -> discord.Message:
        if message_id is not None:
            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                pass
            else:
                if message.author.id == self.bridge.bot_user_id():
                    await message.edit(view=view, allowed_mentions=agent_session_allowed_mentions())
                    return message
        return await send_agent_session_message(channel, view=view, silent=False)

    async def handle_approval_request(self, approval: RemoteApprovalRequest, *, message_id: int | None = None) -> None:
        session = self.bridge.sessions.get(approval.session_id)
        if session is None or session.thread_id is None:
            logger.warning("Agent session approval for unknown session: %s", approval.session_id)
            return
        if approval.session_epoch != session.session_epoch:
            logger.warning("Agent session approval for stale session epoch: %s", approval.session_id)
            return

        if not session.hello.supports("approval_decision"):
            await self.stays_native(
                session, "Action required in the native TUI; this client does not support answering from Discord."
            )
            return
        if approval.approval_kind != "command" or approval.content_text is not None:
            if approval.content_text is None or not approval_content_displayable(approval.approval_kind, approval.content_text):
                await self.stays_native(session, "Action required in the native TUI; Discord cannot show this request in full.")
                return
        content = self.format_approval_request(approval)
        if approval.command_text is not None and (
            not command_text_displayable(approval.command_text)
            # The reason may be cut short; the command and directory may not.
            or len(self.format_approval_request(dataclasses.replace(approval, reason=None))) > DISCORD_MESSAGE_LIMIT
        ):
            # Never offer to approve a command Discord cannot show exactly and whole.
            await self.stays_native(session, "Action required in the native TUI; Discord cannot show this command in full.")
            return

        channel = self.bridge.thread_channel(session.thread_id)
        if not isinstance(channel, discord.Thread):
            return

        view = ApprovalView(self, approval, content[:DISCORD_MESSAGE_LIMIT])
        message = await self.prompt_message(channel, view, message_id)
        previous = session.pending_approvals.get(approval.approval_id)
        if previous is not None and previous.view is not None:
            previous.view.stop()
        # Answerable from the moment it is posted: Discord shows the reactions one at a time, and a tap on the first
        # one while the bot still adds the next must count.
        session.pending_approvals[approval.approval_id] = PendingRemoteApproval(
            thread_id=session.thread_id,
            message_id=message.id,
            view=view,
        )
        await self.bridge.controls.write_message_reactions(
            message,
            [REACTION_APPROVAL_APPROVE, REACTION_APPROVAL_DENY],
        )
        self.wait_on_operator(session, "Approval required. Review the request in this thread.")

    async def stays_native(self, session: AgentSession, detail: str) -> None:
        self.wait_on_operator(session, detail)
        if session.thread_id is not None:
            await self.bridge.post_thread_notice(session.thread_id, detail)

    def wait_on_operator(self, session: AgentSession, detail: str) -> None:
        session.status_revision += 1
        session.display_state = "waiting"
        session.last_status_message = detail
        self.bridge.controls.show_activity(session)

    async def handle_request_user_input(self, request: RemoteRequestUserInput, *, message_id: int | None = None) -> None:
        session = self.bridge.sessions.get(request.session_id)
        if session is None or session.thread_id is None:
            logger.warning("Agent session request_user_input for unknown session: %s", request.session_id)
            return
        if request.session_epoch != session.session_epoch:
            logger.warning(
                "Agent session request_user_input for stale session epoch: %s",
                request.session_id,
            )
            return

        await self.clear_pending_user_inputs(
            session,
            "Agent session is waiting on a newer prompt.",
        )

        if not session.hello.supports("request_user_input_response"):
            await self.user_input_stays_native(session)
            return

        channel = self.bridge.thread_channel(session.thread_id)
        if not isinstance(channel, discord.Thread):
            return

        if (
            not 1 <= len(request.questions) <= MAX_INPUT_QUESTIONS
            or any(not q.id or len(q.options) > 25 for q in request.questions)
            or len({q.id for q in request.questions}) != len(request.questions)
        ):
            await self.user_input_stays_native(session)
            return
        view = RequestUserInputView(self, session.session_id, request)
        # Reserve space for every selection echo before offering an answerable card.
        answer_budget = sum(len("\nSelected: ") + 2 * INPUT_ANSWER_PREVIEW_LIMIT for _ in request.questions)
        if view.content_length() + answer_budget > 4000:
            await self.user_input_stays_native(session)
            return
        message = await self.prompt_message(channel, view, message_id)
        view.message_id = message.id
        session.pending_user_inputs[request.call_id] = PendingRemoteUserInput(
            thread_id=session.thread_id,
            message_id=message.id,
            turn_id=request.turn_id,
            call_id=request.call_id,
        )
        self.wait_on_operator(session, "Answer required. Use the question controls in this thread.")

    async def user_input_stays_native(self, session: AgentSession) -> None:
        await self.stays_native(session, "Answer required in the native TUI; this prompt cannot use Discord controls.")

    async def user_input_context(
        self,
        interaction: discord.Interaction[BlueBot],
        session_id: str,
        session_epoch: str,
        call_id: str,
        turn_id: str,
        message_id: int | None,
    ) -> tuple[AgentSession, PendingRemoteUserInput] | None:
        if not self.bridge.is_operator(interaction.user):
            await interaction.response.send_message(
                "Only Agent session operators can answer prompts.",
                ephemeral=True,
            )
            return None
        session = self.bridge.sessions.get(session_id)
        if session is None or session.websocket.closed:
            await interaction.response.send_message(
                "Agent session is offline; answer was not delivered. Please try again after reconnecting.",
                ephemeral=True,
            )
            return None
        if error := self.bridge.controls.dispatch_error(session, "request_user_input_response"):
            await interaction.response.send_message(error, ephemeral=True)
            return None
        pending = session.pending_user_inputs.get(call_id)
        if (
            session.session_epoch != session_epoch
            or pending is None
            or pending.turn_id != turn_id
            or pending.message_id != message_id
            or getattr(interaction.channel, "id", None) != pending.thread_id
            or (interaction.message is not None and interaction.message.id != message_id)
            or pending.submitted
            or pending.retired
        ):
            await interaction.response.send_message(
                "This prompt is no longer active.",
                ephemeral=True,
            )
            return None
        return session, pending

    async def handle_request_user_input_interaction(
        self,
        interaction: discord.Interaction[BlueBot],
        session_id: str,
        call_id: str,
        turn_id: str,
        response: dict[str, object],
        *,
        session_epoch: str,
        message_id: int | None,
        cancelled: bool = False,
    ) -> None:
        context = await self.user_input_context(
            interaction,
            session_id,
            session_epoch,
            call_id,
            turn_id,
            message_id,
        )
        if context is None:
            return
        session, pending = context
        if not await defer_control(interaction):
            return
        command_id = str(uuid.uuid4())
        pending_command = PendingRemoteCommand(
            thread_id=pending.thread_id,
            message_id=pending.message_id,
            kind="request_user_input_response",
            input_prompt=pending,
        )
        pending.answered_by, pending.cancelled = str(interaction.user), cancelled
        command = RemoteCommand(
            command_id=command_id,
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            kind="request_user_input_response",
            call_id=call_id,
            turn_id=turn_id,
            response=response,
            issued_by=str(interaction.user.id),
        )
        if error := await self.bridge.controls.dispatch_command(session, command, pending_command, input_pending=pending):
            await interaction.followup.send(error, ephemeral=True)
            return
        async with pending.ui_lock:
            if pending.accepted:
                return  # The agent already accepted this answer, and the card says so.
            if (
                pending.retired
                or self.bridge.sessions.get(session_id) is not session
                or session.pending_user_inputs.get(call_id) is not pending
            ):
                await interaction.followup.send(
                    "Answer sent; this prompt is no longer active. Check the native TUI for its outcome.", ephemeral=True
                )
                return
            await interaction.edit_original_response(
                content=None,
                view=notice_card(self.format_request_user_input_pending(interaction.user, cancelled=cancelled)),
                allowed_mentions=agent_session_allowed_mentions(),
            )

    async def handle_approval_interaction(
        self,
        interaction: discord.Interaction[BlueBot],
        session_id: str,
        approval_id: str,
        decision: Literal["approved", "denied"],
        *,
        session_epoch: str,
    ) -> None:
        if not self.bridge.is_operator(interaction.user):
            await interaction.response.send_message(
                "Only Agent session operators can respond to approvals.",
                ephemeral=True,
            )
            return

        session = self.bridge.sessions.get(session_id)
        if session is None or session.websocket.closed:
            await interaction.response.send_message(
                "Agent session is offline; approval was not delivered. Please try again after reconnecting.",
                ephemeral=True,
            )
            return

        pending = session.pending_approvals.get(approval_id)
        if (
            pending is None
            or session.session_epoch != session_epoch
            or interaction.message is None
            or interaction.message.id != pending.message_id
            or getattr(interaction.channel, "id", None) != pending.thread_id
        ):
            await interaction.response.send_message(
                "This approval is no longer active.",
                ephemeral=True,
            )
            return

        if pending.decision is not None:
            await interaction.response.send_message("This approval has already been answered.", ephemeral=True)
            return

        if not await defer_control(interaction):
            return
        if error := await self.dispatch_approval(session, pending, approval_id, decision, interaction.user.id):
            await interaction.followup.send(error, ephemeral=True)
            return
        if pending.view is not None:
            pending.view.stop()
        async with pending.ui_lock:
            if pending.retired or self.bridge.sessions.get(session_id) is not session:
                await interaction.followup.send(
                    "Decision sent; this approval is no longer active. Check the native TUI for its outcome.", ephemeral=True
                )
                return
            content = self.format_approval_pending(decision, interaction.user)
            is_v2 = message_uses_components_v2(interaction.message)
            await interaction.edit_original_response(
                content=None if is_v2 else content,
                view=notice_card(content) if is_v2 else None,
                allowed_mentions=agent_session_allowed_mentions(),
            )
            await self.bridge.controls.clear_message_reactions(interaction.message)

    async def handle_approval_reaction(
        self,
        session: AgentSession,
        thread: discord.Thread,
        approval_id: str,
        emoji: str,
        user: discord.User | discord.Member,
    ) -> bool:
        if emoji not in {REACTION_APPROVAL_APPROVE, REACTION_APPROVAL_DENY}:
            return False

        pending = session.pending_approvals.get(approval_id)
        if pending is None:
            await self.bridge.post_thread_notice(thread.id, "This approval is no longer active.")
            return True
        if pending.decision is not None:
            await self.bridge.controls.remove_message_reaction(thread, pending.message_id, emoji, user)
            return True
        if session.websocket.closed:
            await self.bridge.controls.remove_message_reaction(thread, pending.message_id, emoji, user)
            await self.bridge.post_thread_notice(
                thread.id, "Agent session is offline; approval was not delivered. Please try again after reconnecting."
            )
            return True

        decision: Literal["approved", "denied"]
        if emoji == REACTION_APPROVAL_APPROVE:
            decision = "approved"
        else:
            decision = "denied"

        if error := await self.dispatch_approval(session, pending, approval_id, decision, user.id):
            await self.bridge.post_thread_notice(thread.id, error)
            return True
        await self.edit_approval_message(
            pending,
            self.format_approval_pending(decision, user),
            only_active=True,
        )
        return True

    async def handle_approval_decision_ack(self, payload: dict[str, object]) -> None:
        session_id = payload_string(payload, "session_id")
        approval_id = payload_string(payload, "approval_id")
        session = self.bridge.sessions.get(session_id)
        if session is None or not approval_id:
            return
        await self.retire_approval(
            session, approval_id, lambda pending: self.format_approval_finished(pending.decision, pending.decided_by)
        )

    async def handle_user_input_ack(self, payload: dict[str, object]) -> str | None:
        """Close a question card once the agent accepts its Discord answer; return the closed card's call ID."""
        session = self.bridge.sessions.get(payload_string(payload, "session_id"))
        command = session.pending_commands.get(payload_string(payload, "command_id")) if session is not None else None
        if session is None or command is None or command.kind != "request_user_input_response":
            return None
        pending = command.input_prompt
        if pending is None or session.pending_user_inputs.get(pending.call_id) is not pending:
            return None
        session.pending_user_inputs.pop(pending.call_id)
        pending.accepted = True
        await self.retire_user_input(session, pending, self.format_request_user_input_finished(pending))
        return pending.call_id

    async def handle_approval_decision_reject(self, payload: dict[str, object]) -> None:
        session_id = payload_string(payload, "session_id")
        approval_id = payload_string(payload, "approval_id")
        reason = payload_string(payload, "reason", "approval was rejected")
        session = self.bridge.sessions.get(session_id)
        if session is None or not approval_id:
            return
        await self.retire_approval(session, approval_id, f"**Approval expired**\n{reason}")

    async def retire_approval(
        self,
        session: AgentSession,
        approval_id: str,
        content: str | Callable[[PendingRemoteApproval], str],
    ) -> None:
        pending = session.pending_approvals.pop(approval_id, None)
        if pending is None:
            return
        pending.retired = True
        await self.edit_approval_message(pending, content if isinstance(content, str) else content(pending))

    async def edit_approval_message(self, pending: PendingRemoteApproval, content: str, *, only_active: bool = False) -> None:
        if pending.view is not None:
            pending.view.stop()
        async with pending.ui_lock:
            if only_active and pending.retired:
                return
            channel = self.bridge.thread_channel(pending.thread_id)
            if not isinstance(channel, discord.Thread):
                return
            try:
                message = await channel.fetch_message(pending.message_id)
                await edit_agent_session_message(
                    message,
                    content=content[:DISCORD_MESSAGE_LIMIT],
                )
                await self.bridge.controls.clear_message_reactions(message)
            except discord.DiscordException:
                logger.warning("Unable to edit Agent session approval message %s", pending.message_id)

    async def retire_user_input(self, session: AgentSession, pending: PendingRemoteUserInput, content: str) -> None:
        pending.retired = True
        for command_id, command in list(session.pending_commands.items()):
            if command.input_prompt is pending:
                session.pending_commands.pop(command_id, None)
                if session.active_command_id == command_id:
                    session.active_command_id = None
        async with pending.ui_lock:
            channel = self.bridge.thread_channel(pending.thread_id)
            if not isinstance(channel, discord.Thread):
                return
            try:
                message = await channel.fetch_message(pending.message_id)
                await edit_agent_session_message(message, content=content[:DISCORD_MESSAGE_LIMIT])
                await self.bridge.controls.clear_message_reactions(message)
            except discord.DiscordException:
                logger.warning("Unable to retire Agent session input message %s", pending.message_id)

    async def clear_pending_user_inputs(self, session: AgentSession, content: str) -> None:
        pending_items = list(session.pending_user_inputs.values())
        session.pending_user_inputs.clear()
        for pending in pending_items:
            await self.retire_user_input(session, pending, content)

    async def handle_prompt_resolved(self, message_type: str, payload: dict[str, object]) -> None:
        session_id, epoch = payload.get("session_id"), payload.get("session_epoch")
        if not isinstance(session_id, str) or not isinstance(epoch, str):
            return
        session = self.bridge.sessions.get(session_id)
        if session is None or session.session_epoch != epoch:
            return
        # The websocket event loop awaits each handler. Requests finish registration
        # before their following resolution event; do not dispatch frames as tasks.
        if message_type == "approval_resolved":
            approval_id = payload.get("approval_id")
            if not isinstance(approval_id, str) or not approval_id:
                return
            await self.retire_approval(session, approval_id, "**Resolved**")
        elif message_type == "request_user_input_resolved":
            call_id, turn_id = payload.get("call_id"), payload.get("turn_id")
            if not isinstance(call_id, str) or not call_id or not isinstance(turn_id, str):
                return
            pending = session.pending_user_inputs.get(call_id)
            if pending is None or pending.turn_id != turn_id:
                return
            session.pending_user_inputs.pop(call_id)
            await self.retire_user_input(session, pending, "**Resolved**")

    @staticmethod
    def format_approval_request(approval: RemoteApprovalRequest) -> str:
        """The approval message, untruncated; a raw command_text is shown verbatim instead of the re-quoted argv."""
        if approval.content_text is not None:
            return format_content_approval(approval.approval_kind, approval.content_text)
        if approval.command_text is not None:
            command = approval.command_text
        else:
            command = shlex.join(approval.command) if approval.command else ""
        parts = [
            "**Approval requested**",
            "Quick review: `✅` approve · `✖️` deny",
            "",
            f"```sh\n{command[:APPROVAL_COMMAND_DISPLAY_LIMIT]}\n```",
        ]
        if approval.cwd:
            parts.append(f"cwd: `{approval.cwd}`")
        if approval.reason:
            parts.extend(["", approval.reason[:500]])
        return "\n".join(parts)

    @staticmethod
    def format_approval_pending(
        decision: Literal["approved", "denied"],
        user: discord.User | discord.Member,
    ) -> str:
        label = "Approval sent" if decision == "approved" else "Denial sent"
        return f"**{label}**\nWaiting for local agent to accept the decision.\nby: `{user}`"

    @staticmethod
    def format_approval_finished(decision: str | None, decided_by: int | None) -> str:
        label = f"Submitted: {decision}" if decision in {"approved", "denied"} else "Decision acknowledged"
        by = f"\nby: `{decided_by}`" if decided_by is not None else ""
        return f"**{label}**{by}"

    @staticmethod
    def format_request_user_input_finished(pending: PendingRemoteUserInput) -> str:
        label = "Answer cancelled" if pending.cancelled else "Answered"
        by = f"\nby: `{pending.answered_by}`" if pending.answered_by else ""
        return f"**{label}**{by}"

    @staticmethod
    def format_request_user_input_pending(
        user: discord.User | discord.Member,
        *,
        cancelled: bool = False,
    ) -> str:
        label = "Answer cancelled" if cancelled else "Answer sent"
        return f"**{label}**\nWaiting for local agent to accept the response.\nby: `{user}`"
