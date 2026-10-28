from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import replace
from io import BytesIO
from typing import TYPE_CHECKING

import discord

from discord_blue.doodads.agent_session.cards import notice_card
from discord_blue.doodads.agent_session.formatting import advance_code_fence, format_user_message
from discord_blue.doodads.agent_session.messages import agent_session_allowed_mentions, send_agent_session_message
from discord_blue.doodads.agent_session.reply_modal import answer_excerpt, reply_context_error
from discord_blue.doodads.agent_session.store import StoredQuestion

if TYPE_CHECKING:
    from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
    from discord_blue.doodads.agent_session.sessions import AgentSession

logger = logging.getLogger(__name__)
QUESTION_START = re.compile(r"^\*\*(Q\d+)(?:[.:](?:\*\*|\s)|\*\*[:.\s])")


def detect_questions(text: str) -> tuple[tuple[str, str], ...]:
    starts: list[tuple[int, str]] = []
    lines = text.splitlines()
    fence = None
    for index, line in enumerate(lines):
        if fence is None and (match := QUESTION_START.match(line)) and "?" in line:
            starts.append((index, match[1]))
        fence = advance_code_fence(line, fence)
    return tuple(
        (number, "\n".join(lines[start : starts[index + 1][0] if index + 1 < len(starts) else len(lines)]).strip())
        for index, (start, number) in enumerate(starts)
    )


def source_signature(message: discord.Message, bot_id: int) -> str:
    if not answer_excerpt(message, bot_id):
        return ""
    return hashlib.sha256(repr((message.content, [c.to_dict() for c in message.components])).encode()).hexdigest()


class QuestionCards:
    def __init__(self, bridge: AgentSessionBridge) -> None:
        self.bridge = bridge
        self.lock = asyncio.Lock()

    def record(self, session_id: str, key: str) -> StoredQuestion | None:
        stored = self.bridge.store.records.get(session_id)
        return next((q for q in stored.questions if q.key == key), None) if stored else None

    async def save(self, session_id: str, question: StoredQuestion) -> None:
        stored = self.bridge.store.records[session_id]
        questions = (*(q for q in stored.questions if q.key != question.key), question)
        self.bridge.store.put(session_id, replace(stored, questions=questions))
        await self.bridge.store.flush()

    def restore(self) -> None:
        for session_id, stored in self.bridge.store.records.items():
            for question in stored.questions:
                if question.message_id is not None:
                    self.bridge.bot.add_view(self.view(session_id, question), message_id=question.message_id)

    def view(self, session_id: str, question: StoredQuestion) -> discord.ui.LayoutView:
        text = "\n\n".join(body for _, body in question.questions)
        if len(text.encode("utf-16-le")) // 2 > 3000:
            text = (
                " · ".join(number for number, _ in question.questions)[:1000]
                + "\nFull questions, recommendations, costs and alternatives are in questions.md attached to this card."
            )
        view = notice_card(f"### Question\n{text}\n\n[Open session answer]({question.source_url})\n**{question.state}**")
        container = view.children[0]
        assert isinstance(container, discord.ui.Container)
        container.add_item(discord.ui.ActionRow(QuestionAnswerButton(self, session_id, question)))
        return view

    def context_error(self, interaction: discord.Interaction, session_id: str, question: StoredQuestion | None) -> str | None:
        if question is None:
            return "This question is no longer available."
        session = self.bridge.sessions.get(session_id)
        error = reply_context_error(self.bridge, interaction, session, question.epoch, thread_id=question.thread_id)
        if error:
            return error
        if getattr(interaction.channel, "id", None) != question.parent_id:
            return "Open the original Question card in its channel."
        if interaction.message is not None and interaction.message.id != question.message_id:
            return "Open the original Question card."
        if question.state != "Waiting":
            return f"This answer was already submitted: {question.state}. Check the session before replying again."
        return None

    async def post(self, session: AgentSession, thread: discord.Thread, text: str, source: discord.Message) -> None:
        questions = detect_questions(text)
        parent = getattr(thread, "parent", None)
        if not questions or not isinstance(parent, discord.TextChannel):
            return
        key = hashlib.sha256(f"{session.session_id}\0{text}".encode()).hexdigest()[:32]
        async with self.lock:
            if self.record(session.session_id, key) is not None:
                return
            if session.session_id not in self.bridge.store.records:
                self.bridge.save_session(session, "live")
            question = StoredQuestion(
                key,
                session.session_epoch,
                thread.id,
                parent.id,
                source.jump_url,
                questions,
                source_signature(source, self.bridge.bot_user_id() or 0),
            )
            try:
                await self.save(session.session_id, question)
                view = self.view(session.session_id, question)
                full = "\n\n".join(body for _, body in questions)
                if len(full.encode("utf-16-le")) // 2 > 3000:
                    attachment = discord.File(BytesIO(full.encode()), filename="questions.md")
                    try:
                        message = await parent.send(
                            view=view, file=attachment, silent=True, allowed_mentions=agent_session_allowed_mentions()
                        )
                    finally:
                        attachment.close()
                else:
                    message = await send_agent_session_message(parent, view=view, silent=True)
                await self.save(session.session_id, replace(question, message_id=message.id))
            except (OSError, discord.DiscordException):
                logger.exception("Unable to confirm Question card publication for session %s", session.session_id)

    def context_error_for_delivery(self, session: AgentSession, question: StoredQuestion) -> str | None:
        if session.session_epoch != question.epoch or not session.acknowledged or session.thread_id != question.thread_id:
            return "The session changed; answer was not sent."
        return self.bridge.controls.dispatch_error(session, "reply")

    async def outcome(self, session_id: str, key: str, state: str) -> None:
        question = self.record(session_id, key)
        if question is None:
            return
        question = replace(question, state=state)
        await self.save(session_id, question)
        parent = self.bridge.bot.get_channel(question.parent_id)
        if isinstance(parent, discord.TextChannel) and question.message_id is not None:
            try:
                await parent.get_partial_message(question.message_id).edit(
                    view=self.view(session_id, question), allowed_mentions=agent_session_allowed_mentions()
                )
            except discord.DiscordException:
                logger.warning("Unable to update Question card %s", question.message_id)


class QuestionAnswerButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(self, cards: QuestionCards, session_id: str, question: StoredQuestion) -> None:
        super().__init__(label="Answer", custom_id=f"dui:q:{question.key}", disabled=question.state != "Waiting")
        self.cards, self.session_id, self.key = cards, session_id, question.key

    async def callback(self, interaction: discord.Interaction) -> None:
        current = self.cards.record(self.session_id, self.key)
        error = self.cards.context_error(interaction, self.session_id, current)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        assert current is not None
        await interaction.response.send_modal(QuestionAnswerModal(self.cards, self.session_id, current, interaction.user.id))


class QuestionAnswerModal(discord.ui.Modal):
    def __init__(self, cards: QuestionCards, session_id: str, question: StoredQuestion, user_id: int) -> None:
        super().__init__(title="Answer questions", timeout=600)
        self.cards, self.session_id, self.question, self.user_id = cards, session_id, question, user_id
        self.fields: list[discord.ui.TextInput[QuestionAnswerModal]] = []
        self.combined = len(question.questions) > 4
        self.add_item(discord.ui.TextDisplay(f"[Read questions and recommendations]({question.source_url})"))
        groups = [("All answers with Q numbers", "")] if self.combined else list(question.questions)
        for number, body in groups:
            field = discord.ui.TextInput[QuestionAnswerModal](
                style=discord.TextStyle.paragraph, max_length=1800 // (2 * len(groups))
            )
            self.fields.append(field)
            self.add_item(discord.ui.Label(text=number[:45], description=body[:100] or None, component=field))
        if self.combined:
            self.fields[0].default = "\n".join(f"{number}: " for number, _ in question.questions)[:900]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.cards.lock:
            question = self.cards.record(self.session_id, self.question.key)
            error = self.cards.context_error(interaction, self.session_id, question)
            if error or interaction.user.id != self.user_id:
                await interaction.followup.send(error or "This form belongs to another operator.", ephemeral=True)
                return
            if any(not field.value.strip() for field in self.fields):
                await interaction.followup.send("Answer every question before submitting.", ephemeral=True)
                return
            text = (
                self.fields[0].value
                if self.combined
                else "\n\n".join(
                    f"{number}: {field.value.strip()}"
                    for (number, _), field in zip(self.question.questions, self.fields, strict=True)
                )
            )
            if self.combined and any(
                re.search(rf"(?m)^{re.escape(number)}:[ \t]*\S[^\n]*$", text) is None for number, _ in self.question.questions
            ):
                await interaction.followup.send("Keep each Q number followed by a colon and its answer.", ephemeral=True)
                return
            assert question is not None
            session = self.cards.bridge.sessions.get(self.session_id)
            assert session is not None
            thread = self.cards.bridge.thread_channel(question.thread_id)
            if not isinstance(thread, discord.Thread):
                await interaction.followup.send("The session thread is unavailable; answer was not sent.", ephemeral=True)
                return
            try:
                source = await thread.fetch_message(int(question.source_url.rsplit("/", 1)[1]))
                if source_signature(source, self.cards.bridge.bot_user_id() or 0) != question.source_signature:
                    await interaction.followup.send("The source answer changed; answer was not sent.", ephemeral=True)
                    return
                if error := self.cards.context_error_for_delivery(session, question):
                    await interaction.followup.send(error, ephemeral=True)
                    return
                await self.cards.outcome(self.session_id, question.key, "Uncertain")
                message = await send_agent_session_message(thread, format_user_message(text), silent=True)
                error = self.cards.context_error_for_delivery(session, question)
                if error is None:
                    error = await self.cards.bridge.controls.send_reply(
                        session, thread, interaction.user, text, message.id, question_key=question.key
                    )
            except (OSError, discord.DiscordException):
                logger.exception("Unable to confirm Question answer submission")
                error = "Answer delivery is uncertain. Check the session before replying again."
            await interaction.followup.send(
                error or "Answer sent; waiting for native confirmation. The card shows its status.", ephemeral=True
            )
