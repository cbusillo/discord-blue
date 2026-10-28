from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import replace
from collections.abc import Iterator
from io import BytesIO
from typing import TYPE_CHECKING

import discord

from discord_blue.doodads.agent_session.cards import (
    ANSWER_CARD_COMPONENT_ID,
    ANSWER_CONTINUATION_COMPONENT_ID,
    STATUS_LABELS,
    notice_card,
)
from discord_blue.doodads.agent_session.controls import DELIVERY_UNCONFIRMED
from discord_blue.doodads.agent_session.formatting import (
    advance_code_fence,
    format_user_message,
    mark_assistant_message,
    markdown_table_end,
)
from discord_blue.doodads.agent_session.messages import (
    agent_session_allowed_mentions,
    send_agent_session_message,
    send_assistant_message,
)
from discord_blue.doodads.agent_session.reply_modal import answer_text, reply_context_error
from discord_blue.doodads.agent_session.store import SESSION_STORE_WAIT_TIMEOUT_SECONDS, QuestionState, StoredQuestion

if TYPE_CHECKING:
    from discord_blue.doodads.agent_session.bridge import AgentSessionBridge
    from discord_blue.doodads.agent_session.sessions import AgentSession

logger = logging.getLogger(__name__)
QUESTION_START = re.compile(r"^\*\*(Q\d+)(?:[.:](?:\*\*|\s)|\*\*[:.\s])")


def top_level_lines(lines: list[str]) -> Iterator[tuple[int, str]]:
    fence = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if fence is None:
            if line.startswith(">>>"):
                return
            if (end := markdown_table_end(lines, index)) is not None:
                index = end
                continue
            yield index, line
        fence = advance_code_fence(line, fence)
        index += 1


def detect_questions(text: str) -> tuple[tuple[str, str], ...]:
    lines = text.splitlines()
    starts = []
    for index, line in top_level_lines(lines):
        match = QUESTION_START.match(line)
        if match is not None and line.rstrip("* ").endswith("?"):
            starts.append((index, match[1]))
    questions = []
    for index, (start, number) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(lines)
        fence = None
        for position in range(start, end):
            line = lines[position]
            if fence is None and not line.strip() and position + 1 < end:
                following = lines[position + 1]
                if following.strip() and not re.match(
                    r"(?i)^(?:\*\*)?(?:recommendation|cost|ongoing cost|alternatives?|options?|why|trade-offs?)\b|^[-*+0-9]",
                    following,
                ):
                    end = position
                    break
            fence = advance_code_fence(line, fence)
        questions.append((number, "\n".join(lines[start:end]).strip()))
    return tuple(questions)


def source_signature(message: discord.Message, bot_id: int) -> str:
    text = answer_text(message, bot_id)
    return hashlib.sha256(text.encode()).hexdigest() if text else ""


class QuestionCards:
    def __init__(self, bridge: AgentSessionBridge) -> None:
        self.bridge = bridge
        self.locks: dict[str, asyncio.Lock] = {}

    def lock(self, key: str) -> asyncio.Lock:
        return self.locks.setdefault(key, asyncio.Lock())

    def record(self, session_id: str, key: str) -> StoredQuestion | None:
        stored = self.bridge.store.records.get(session_id)
        return next((q for q in stored.questions if q.key == key), None) if stored else None

    def save(self, session_id: str, question: StoredQuestion) -> bool:
        stored = self.bridge.store.records.get(session_id)
        if stored is None or not self.bridge.store.accepts_writes:
            return False
        questions = (*(q for q in stored.questions if q.key != question.key), question)
        self.bridge.store.put(session_id, replace(stored, questions=questions))
        return True

    async def persist(self) -> bool:
        try:
            await self.bridge.threads.bounded(self.bridge.store.flush(), SESSION_STORE_WAIT_TIMEOUT_SECONDS)
            return self.bridge.store.accepts_writes
        except (OSError, TimeoutError):
            logger.exception("Question reservation is not durable; no new delivery attempted")
            return False

    def restore(self) -> None:
        for session_id, stored in self.bridge.store.records.items():
            for question in stored.questions:
                if question.message_id is not None:
                    self.bridge.bot.add_view(self.view(session_id, question), message_id=question.message_id)

    def view(self, session_id: str, question: StoredQuestion) -> discord.ui.LayoutView:
        if question.state == QuestionState.ANSWERED:
            answer = discord.utils.escape_markdown(" ".join(question.answer.split())[:600])
            numbers = ", ".join(number for number, _ in question.questions)
            view = notice_card(f"**{numbers}** · {answer} · [Decision]({question.decision_url})")
            container = view.children[0]
            assert isinstance(container, discord.ui.Container)
            container.accent_color = STATUS_LABELS["done"][1]
            container.id = ANSWER_CARD_COMPONENT_ID if question.complete else ANSWER_CONTINUATION_COMPONENT_ID
            return view
        text = "\n\n".join(body for _, body in question.questions)
        if len(text.encode("utf-16-le")) // 2 > 3000:
            text = " · ".join(body.split("\n", 1)[0] for _, body in question.questions)[:1000] + (
                "\nFull questions, recommendations, costs and alternatives are attached below."
                if question.has_attachment
                else "\nFull context remains in the native terminal."
            )
        view = notice_card("")
        container = view.children[0]
        assert isinstance(container, discord.ui.Container)
        container.clear_items()
        container.accent_color = STATUS_LABELS["waiting"][1]
        container.id = ANSWER_CARD_COMPONENT_ID if question.complete else ANSWER_CONTINUATION_COMPONENT_ID
        container.add_item(discord.ui.TextDisplay(mark_assistant_message(f"### Question\n{text}")))
        state = "Unanswered · Session ended" if question.state == QuestionState.CLOSED else question.state
        container.add_item(discord.ui.TextDisplay(f"**{state}**"))
        if question.has_attachment:
            container.add_item(discord.ui.File("attachment://questions.md"))
        if question.state != QuestionState.CLOSED:
            container.add_item(discord.ui.ActionRow(QuestionAnswerButton(self, session_id, question)))
        return view

    async def send_answer(self, session: AgentSession, thread: discord.Thread, text: str) -> None:
        await self.decisions(session.session_id, text, recorded=True)
        questions = detect_questions(text)
        remaining = text
        for _number, body in questions:
            before, _, remaining = remaining.partition(body)
            if before.strip():
                await send_assistant_message(thread, before, silent=True, complete=False, full_text=text)
            await self.post(session, thread, body, complete=not remaining.strip())
        if remaining.strip():
            await send_assistant_message(thread, remaining, silent=bool(questions), full_text=text)

    async def decisions(self, session_id: str, text: str, *, url: str = "", recorded: bool = False) -> None:
        stored = self.bridge.store.records.get(session_id)
        if stored is None:
            return
        pattern = r"(?m)^(?:Director decision:[ \t]*)?(Q\d+)[.: \t]+([^\n]+)"
        if recorded:
            pattern = r"(?m)^Director decision:[ \t]*(Q\d+)[.: \t]+([^\n]+)"
        lines = text.splitlines()
        matches = []
        for index, line in top_level_lines(lines):
            match = re.match(pattern, line)
            if match is not None:
                matches.append((index, match))
        for index, (start, match) in enumerate(matches):
            end = matches[index + 1][0] if index + 1 < len(matches) else len(lines)
            context = "\n".join(lines[start:end])
            link = re.search(r"https://github\.com/[^\s)]+#issuecomment-\d+", context) if recorded else None
            decision_url = link[0] if link else url
            if not decision_url:
                continue
            for question in tuple(self.bridge.store.records[session_id].questions):
                if question.state == QuestionState.ANSWERED or match[1] not in {number for number, _ in question.questions}:
                    continue
                async with self.lock(question.key):
                    current = self.record(session_id, question.key)
                    if current is None or current.state == QuestionState.ANSWERED:
                        continue
                    answered = replace(current, state=QuestionState.ANSWERED, answer=match[2].strip(), decision_url=decision_url)
                    self.save(session_id, answered)
                    await self.refresh(session_id, [answered])

    def context_error(self, interaction: discord.Interaction, session_id: str, question: StoredQuestion | None) -> str | None:
        if question is None:
            return "This question is no longer available."
        session = self.bridge.sessions.get(session_id)
        if session is not None and session.session_epoch != question.epoch:
            return "This question belongs to an earlier conversation. Reply in the current session thread instead."
        error = reply_context_error(
            self.bridge, interaction, session, question.epoch, thread_id=question.thread_id, reopen_action="the Question card"
        )
        if error:
            return error
        if getattr(interaction.channel, "id", None) != question.thread_id:
            return "Open the original Question card in its session thread."
        if interaction.message is not None and interaction.message.id != question.message_id:
            return "Open the original Question card."
        if question.state != QuestionState.WAITING:
            return f"This answer was already submitted: {question.state}. Check the session before replying again."
        return None

    async def post(self, session: AgentSession, thread: discord.Thread, text: str, *, complete: bool = True) -> None:
        questions = detect_questions(text)
        if not questions:
            return
        key = hashlib.sha256(f"{session.session_id}\0{session.session_epoch}\0{questions[0][0]}".encode()).hexdigest()[:32]
        if self.record(session.session_id, key) is not None:
            return
        if session.session_id not in self.bridge.store.records:
            self.bridge.save_session(session, "live")
        question = StoredQuestion(
            key,
            session.session_epoch,
            thread.id,
            "",
            questions,
            complete=complete,
        )
        question = replace(
            question,
            has_attachment=sum(len(body.encode("utf-16-le")) // 2 for _, body in questions) + 2 * (len(questions) - 1) > 3000,
        )
        publication_started = False
        try:
            if not self.save(session.session_id, question) or not await self.persist():
                await send_assistant_message(thread, text, silent=True, complete=complete)
                await send_agent_session_message(
                    thread, "The Question card could not be saved. Reply in this session thread instead.", silent=True
                )
                return
            current = self.record(session.session_id, key)
            if current is None or current.state != QuestionState.WAITING:
                return
            view = self.view(session.session_id, question)
            full = "\n\n".join(body for _, body in questions)
            publication_started = True
            if len(full.encode("utf-16-le")) // 2 > 3000:
                attachment = discord.File(BytesIO(full.encode()), filename="questions.md")
                try:
                    message = await thread.send(
                        view=view, file=attachment, silent=True, allowed_mentions=agent_session_allowed_mentions()
                    )
                except discord.Forbidden:
                    question = replace(self.record(session.session_id, key) or question, has_attachment=False)
                    self.save(session.session_id, question)
                    message = await send_agent_session_message(thread, view=self.view(session.session_id, question), silent=True)
                finally:
                    attachment.close()
            else:
                message = await send_agent_session_message(thread, view=view, silent=True)
            current = self.record(session.session_id, key) or question
            current = replace(
                current,
                message_id=message.id,
                source_url=message.jump_url,
                sources=((message.id, source_signature(message, self.bridge.bot_user_id() or 0)),),
            )
            self.save(session.session_id, current)
            if current.state != QuestionState.WAITING:
                await self.refresh(session.session_id, [current])
        except (OSError, discord.DiscordException) as exc:
            logger.exception("Unable to confirm Question card publication for session %s", session.session_id)
            if not publication_started or (isinstance(exc, discord.HTTPException) and 400 <= exc.status < 500):
                stored = self.bridge.store.records[session.session_id]
                self.bridge.store.put(
                    session.session_id, replace(stored, questions=tuple(q for q in stored.questions if q.key != key))
                )
                try:
                    await send_agent_session_message(
                        thread,
                        "The Question card could not be posted in the thread. Reply in this session thread instead.",
                        silent=True,
                    )
                except (OSError, discord.DiscordException):
                    logger.exception("Unable to report failed Question card publication")
            else:
                try:
                    await send_agent_session_message(
                        thread, "The Question card may not have posted. Check this thread before replying.", silent=True
                    )
                except (OSError, discord.DiscordException):
                    logger.warning("Unable to report uncertain Question publication")

    async def close(self, session_id: str, *, current_epoch: str | None = None) -> None:
        stored = self.bridge.store.records.get(session_id)
        closed = (
            [
                replace(q, state=QuestionState.CLOSED)
                for q in stored.questions
                if q.state == QuestionState.WAITING and q.epoch != current_epoch
            ]
            if stored
            else []
        )
        for question in closed:
            self.save(session_id, question)
        await self.refresh(session_id, closed)

    async def outcome(self, session_id: str, key: str, state: QuestionState) -> None:
        question = self.record(session_id, key)
        if question is not None and question.state != QuestionState.ANSWERED:
            question = replace(question, state=state)
            self.save(session_id, question)
            await self.refresh(session_id, [question])

    async def refresh(self, session_id: str, questions: list[StoredQuestion]) -> None:
        async def edit_cards() -> None:
            for question in questions:
                channel = self.bridge.thread_channel(question.thread_id)
                if isinstance(channel, discord.Thread) and question.message_id is not None:
                    try:
                        await channel.get_partial_message(question.message_id).edit(
                            view=self.view(session_id, question), allowed_mentions=agent_session_allowed_mentions()
                        )
                    except (OSError, discord.DiscordException):
                        logger.warning("Unable to update Question card %s", question.message_id)

        if questions:
            try:
                await self.bridge.threads.bounded(edit_cards(), SESSION_STORE_WAIT_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.warning("Question card updates are still pending for session %s", session_id)


class QuestionAnswerButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(self, cards: QuestionCards, session_id: str, question: StoredQuestion) -> None:
        super().__init__(label="Answer", custom_id=f"dui:q:{question.key}", disabled=question.state != QuestionState.WAITING)
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
        self.session = cards.bridge.sessions.get(session_id)
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
        async with self.cards.lock(self.question.key):
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
            session = self.session
            if session is None or self.cards.bridge.sessions.get(self.session_id) is not session:
                await interaction.followup.send("The session reconnected; reopen the Question card.", ephemeral=True)
                return
            thread = self.cards.bridge.thread_channel(question.thread_id)
            if not isinstance(thread, discord.Thread):
                await interaction.followup.send("The session thread is unavailable; answer was not sent.", ephemeral=True)
                return
            dispatch_started = False
            reserved = False
            message = None
            try:
                for message_id, signature in question.sources:
                    source = await thread.fetch_message(message_id)
                    if source_signature(source, self.cards.bridge.bot_user_id() or 0) != signature:
                        await interaction.followup.send("The source answer changed; answer was not sent.", ephemeral=True)
                        return
                if error := reply_context_error(
                    self.cards.bridge,
                    interaction,
                    session,
                    question.epoch,
                    thread_id=question.thread_id,
                    reopen_action="the Question card",
                ):
                    await interaction.followup.send(error, ephemeral=True)
                    return
                await self.cards.outcome(self.session_id, question.key, QuestionState.UNCERTAIN)
                reserved = True
                if not await self.cards.persist():
                    await self.cards.outcome(self.session_id, question.key, QuestionState.NOT_DELIVERED)
                    await interaction.followup.send("The answer could not be saved; it was not delivered.", ephemeral=True)
                    return
                message = await send_agent_session_message(thread, format_user_message(text), silent=True)
                answered = replace(
                    question,
                    state=QuestionState.ANSWERED,
                    answer=self.fields[0].value.strip(),
                    decision_url=message.jump_url,
                )
                self.cards.save(self.session_id, answered)
                await self.cards.refresh(self.session_id, [answered])
                error = reply_context_error(
                    self.cards.bridge,
                    interaction,
                    session,
                    question.epoch,
                    thread_id=question.thread_id,
                    reopen_action="the Question card",
                )
                if error is None:
                    dispatch_started = True
                    error = await self.cards.bridge.controls.send_reply(
                        session, thread, interaction.user, text, message.id, question_key=question.key
                    )
            except (OSError, discord.DiscordException):
                logger.exception("Unable to confirm Question answer submission")
                error = DELIVERY_UNCONFIRMED if dispatch_started else "Answer was not delivered."
            if error is not None:
                if reserved and error != DELIVERY_UNCONFIRMED:
                    try:
                        await self.cards.outcome(self.session_id, question.key, QuestionState.NOT_DELIVERED)
                    except OSError:
                        logger.exception("Unable to persist definite Question answer failure")
                if message is not None:
                    try:
                        await send_agent_session_message(thread, error, silent=True)
                    except discord.DiscordException:
                        logger.warning("Unable to post Question answer failure in thread %s", thread.id)
            await interaction.followup.send(
                error or "Answer sent; waiting for native confirmation. The card shows its status.", ephemeral=True
            )
