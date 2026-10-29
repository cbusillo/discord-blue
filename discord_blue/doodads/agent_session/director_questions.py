from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, replace
from collections.abc import Iterator
from io import BytesIO
from typing import TYPE_CHECKING

import discord

from discord_blue.doodads.agent_session.cards import (
    ANSWER_CHUNK_LIMIT,
    ANSWER_CARD_COMPONENT_ID,
    ANSWER_CONTINUATION_COMPONENT_ID,
    STATUS_LABELS,
    assistant_answer_cards,
    notice_card,
    question_boundary,
)
from discord_blue.doodads.agent_session.chunks import split_discord_message
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


@dataclass(frozen=True)
class QuestionSection:
    number: str
    body: str
    start: int
    end: int


def detect_questions(text: str) -> tuple[tuple[str, str], ...]:
    return tuple((section.number, section.body) for section in question_sections(text))


def question_sections(text: str) -> Iterator[QuestionSection]:
    source_lines = text.splitlines(keepends=True)
    lines = [line.rstrip("\r\n") for line in source_lines]
    offsets = [0]
    for line in source_lines:
        offsets.append(offsets[-1] + len(line))
    starts = []
    for index, line in top_level_lines(lines):
        match = QUESTION_START.match(line)
        if match is not None and line.rstrip("* \r\n").endswith("?"):
            starts.append((index, match[1]))
    for index, (start, number) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(lines)
        fence = None
        for position in range(start, end):
            line = lines[position]
            if fence is None and position > start and question_boundary(line.strip()):
                end = position
                break
            fence = advance_code_fence(line, fence)
        body = "".join(source_lines[start:end]).strip()
        yield QuestionSection(number, body, offsets[start], offsets[start] + len(body))


def source_signature(message: discord.Message, bot_id: int) -> str:
    text = answer_text(message, bot_id)
    return hashlib.sha256(text.encode()).hexdigest() if text else ""


class QuestionCards:
    def __init__(self, bridge: AgentSessionBridge) -> None:
        self.bridge = bridge
        self.locks: dict[str, asyncio.Lock] = {}
        self.edit_locks: dict[str, asyncio.Lock] = {}

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
                    view = self.view(session_id, question)
                    if question.legacy_parent_id is not None:
                        view.add_item(discord.ui.ActionRow(QuestionAnswerButton(self, session_id, question)))
                    self.bridge.bot.add_view(view, message_id=question.message_id)

    def view(self, session_id: str, question: StoredQuestion) -> discord.ui.LayoutView:
        if question.legacy_parent_id is not None:
            numbers = ", ".join(number for number, _ in question.questions)
            return notice_card(f"**{numbers}** · Continue in the [session thread]({question.source_url}).")
        if question.state == QuestionState.ANSWERED:
            answer = discord.utils.escape_markdown(" ".join(question.answer.split())[:120])
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
                else "\nFull context continues in this thread."
            )
        view = notice_card("")
        container = view.children[0]
        assert isinstance(container, discord.ui.Container)
        container.clear_items()
        container.accent_color = STATUS_LABELS["waiting"][1]
        container.id = ANSWER_CARD_COMPONENT_ID if question.complete else ANSWER_CONTINUATION_COMPONENT_ID
        container.add_item(discord.ui.TextDisplay(mark_assistant_message(f"### Question\n{text}")))
        state = "Unanswered" if question.state == QuestionState.CLOSED else question.state
        container.add_item(discord.ui.TextDisplay(f"**{state}**"))
        if question.has_attachment:
            container.add_item(discord.ui.File("attachment://questions.md"))
        if question.state != QuestionState.CLOSED:
            container.add_item(discord.ui.ActionRow(QuestionAnswerButton(self, session_id, question)))
        return view

    async def send_answer(self, session: AgentSession, thread: discord.Thread, text: str, *, replay: bool = False) -> None:
        await self.decisions(session.session_id, text, recorded=True)
        position = 0
        parts: list[tuple[str, str | None]] = []
        sections = list(question_sections(text))
        for section in sections:
            if (before := text[position : section.start]).strip():
                parts.append((before, None))
            parts.append((section.body, section.number))
            position = section.end
        if (remaining := text[position:]).strip():
            parts.append((remaining, None))
        full_text = text
        shown: set[str] = set()
        for index, (body, number) in enumerate(parts):
            if number is not None:
                current = self.record(session.session_id, self.key(session, number))
                if number in shown and current is not None:
                    await self.reference(thread, current, complete=index == len(parts) - 1)
                else:
                    await self.post(session, thread, body, complete=index == len(parts) - 1, replay=replay)
                shown.add(number)
            else:
                await send_assistant_message(
                    thread, body, silent=bool(sections), complete=index == len(parts) - 1, full_text=full_text
                )
                if assistant_answer_cards(body)[1]:
                    full_text = ""

    @staticmethod
    def key(session: AgentSession, number: str) -> str:
        return hashlib.sha256(f"{session.session_id}\0{number}".encode()).hexdigest()[:32]

    async def decisions(self, session_id: str, text: str, *, url: str = "", recorded: bool = False) -> bool:
        stored = self.bridge.store.records.get(session_id)
        if stored is None:
            return False
        changed = False
        pattern = r"(?i)^(Q\d+)[.: \t]+([^\n]+)"
        if recorded:
            pattern = r"(?i)^(?:\*\*)?Director decision:(?:\*\*)?[ \t]*(?:\*\*)?(Q\d+)[.: \t*]+([^\n]+)"
        lines = text.splitlines()
        matches = []
        for index, line in top_level_lines(lines):
            match = re.match(pattern, line)
            if match is not None:
                matches.append((index, match))
        for index, (start, match) in enumerate(matches):
            end = matches[index + 1][0] if index + 1 < len(matches) else len(lines)
            context = "\n".join(lines[start:end]).split("\n\n", 1)[0]
            if not recorded and (match[2].rstrip().endswith("?") or re.search(r"(?i)\bQ\d+\b", match[2])):
                continue
            link = re.search(r"https://github\.com/[^\s)]+#issuecomment-\d+", context) if recorded else None
            decision_url = link[0] if link else url
            if not decision_url:
                continue
            for question in tuple(self.bridge.store.records[session_id].questions):
                if match[1].upper() not in {number for number, _ in question.questions}:
                    continue
                async with self.lock(question.key):
                    current = self.record(session_id, question.key)
                    if current is None or current.state == QuestionState.ANSWERED:
                        continue
                    answered = replace(current, state=QuestionState.ANSWERED, answer=match[2].strip(), decision_url=decision_url)
                    self.save(session_id, answered)
                    changed = True
                    await self.refresh(session_id, [answered])
        return changed

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

    async def post(
        self, session: AgentSession, thread: discord.Thread, text: str, *, complete: bool = True, replay: bool = False
    ) -> None:
        questions = detect_questions(text)
        if not questions:
            return
        key = self.key(session, questions[0][0])
        async with self.lock(key):
            await self.publish(session, thread, text, questions, key, complete=complete, replay=replay)

    async def publish(
        self,
        session: AgentSession,
        thread: discord.Thread,
        text: str,
        questions: tuple[tuple[str, str], ...],
        key: str,
        *,
        complete: bool,
        replay: bool,
    ) -> None:
        current = self.record(session.session_id, key)
        previous = current
        if current is not None and len(questions) == 1 and questions[0][1] in current.questions[0][1]:
            questions = current.questions
        source = None
        published = False
        if current is not None and current.message_id is None:
            await self.fallback(thread, text, "Earlier Question publication is unconfirmed; reply here instead.", complete)
            return
        restored = current is not None and replay and current.questions == questions
        if current is not None and not restored:
            current = replace(
                current,
                questions=questions,
                revision=current.revision + 1,
                epoch=session.session_epoch,
                state=QuestionState.UNCERTAIN,
                answer="",
                decision_url="",
            )
            self.save(session.session_id, current)
        try:
            if current is not None and current.message_id is not None:
                try:
                    source = await thread.fetch_message(current.message_id)
                except discord.NotFound:
                    source = None
            if current is not None and source is not None and restored:
                await self.reference(thread, current, complete=complete)
                return
            if session.session_id not in self.bridge.store.records:
                self.bridge.save_session(session, "live")
            question = StoredQuestion(
                key,
                session.session_epoch,
                thread.id,
                source.jump_url if source is not None else "",
                questions,
                message_id=source.id if source is not None else None,
                revision=current.revision if current is not None else 0,
                complete=current.complete if source is not None and current is not None else complete,
            )
            if restored and current is not None:
                question = replace(question, state=current.state, answer=current.answer, decision_url=current.decision_url)
            if not self.save(session.session_id, replace(question, state=QuestionState.UNCERTAIN)) or not await self.persist():
                await self.fallback(thread, text, "The Question card could not be saved. Reply in this thread instead.", complete)
                return
            async with self.edit_locks.setdefault(key, asyncio.Lock()):
                question, sources = await self.render(session.session_id, thread, question, source, complete=complete)
            question = replace(
                question,
                message_id=sources[0].id,
                source_url=sources[0].jump_url,
                sources=tuple((message.id, source_signature(message, self.bridge.bot_user_id() or 0)) for message in sources),
            )
            self.save(session.session_id, question)
            published = True
            if source is not None:
                await self.reference(thread, question, complete=complete)
        except (OSError, discord.DiscordException) as exc:
            if previous is not None and not published:
                self.save(
                    session.session_id, replace(previous, revision=current.revision if current is not None else previous.revision)
                )
            elif not published and isinstance(exc, discord.HTTPException) and 400 <= exc.status < 500:
                stored = self.bridge.store.records[session.session_id]
                self.bridge.store.put(
                    session.session_id, replace(stored, questions=tuple(q for q in stored.questions if q.key != key))
                )
            logger.exception("Unable to confirm Question publication for session %s", session.session_id)
            await self.fallback(thread, text, "The Question card could not be updated. Check this thread before replying.", complete)

    @staticmethod
    async def reference(thread: discord.Thread, question: StoredQuestion, *, complete: bool) -> None:
        numbers = ", ".join(number for number, _ in question.questions)
        await send_assistant_message(thread, f"[{numbers} · Open question]({question.source_url})", silent=True, complete=complete)

    @staticmethod
    async def fallback(thread: discord.Thread, text: str, notice: str, complete: bool) -> None:
        try:
            await send_assistant_message(thread, text, silent=True, complete=complete)
            await send_agent_session_message(thread, notice, silent=True)
        except (OSError, discord.DiscordException):
            logger.exception("Unable to post Question fallback")

    async def render(
        self,
        session_id: str,
        thread: discord.Thread,
        question: StoredQuestion,
        source: discord.Message | None,
        *,
        complete: bool,
    ) -> tuple[StoredQuestion, list[discord.Message]]:
        full = "\n\n".join(body for _, body in question.questions)
        question = replace(
            question, has_attachment=question.state != QuestionState.ANSWERED and len(full.encode("utf-16-le")) // 2 > 3000
        )
        attachment = discord.File(BytesIO(full.encode()), filename="questions.md") if question.has_attachment else None
        sources: list[discord.Message] = []
        try:
            try:
                if source is not None:
                    updated = await source.edit(
                        view=self.view(session_id, question),
                        attachments=[attachment] if attachment is not None else [],
                        allowed_mentions=agent_session_allowed_mentions(),
                    )
                    source = updated or source
                elif attachment is not None:
                    source = await thread.send(
                        view=self.view(session_id, question),
                        file=attachment,
                        silent=True,
                        allowed_mentions=agent_session_allowed_mentions(),
                    )
                else:
                    source = await send_agent_session_message(thread, view=self.view(session_id, question), silent=True)
                sources.append(source)
            except discord.Forbidden:
                if attachment is None:
                    raise
                question = replace(question, has_attachment=False, complete=False)
                if source is not None:
                    updated = await source.edit(
                        view=self.view(session_id, question), attachments=[], allowed_mentions=agent_session_allowed_mentions()
                    )
                    source = updated or source
                else:
                    source = await send_agent_session_message(thread, view=self.view(session_id, question), silent=True)
                sources.append(source)
                context = full.split("\n", 1)[1] if "\n" in full else ""
                chunks = split_discord_message(context, ANSWER_CHUNK_LIMIT)
                for index, chunk in enumerate(chunks):
                    await send_assistant_message(
                        thread, chunk, silent=True, complete=complete and index == len(chunks) - 1, sources=sources
                    )
        finally:
            if attachment is not None:
                attachment.close()
        return question, sources

    async def close(self, session_id: str, *, current_epoch: str | None = None, thread: discord.Thread | None = None) -> None:
        stored = self.bridge.store.records.get(session_id)
        for previous in stored.questions if stored is not None else ():
            async with self.lock(previous.key):
                question = self.record(session_id, previous.key)
                if question is None:
                    continue
                if question.legacy_parent_id is None and (
                    question.epoch == current_epoch or question.state not in {QuestionState.WAITING, QuestionState.CLOSED}
                ):
                    continue
                question = replace(question, state=QuestionState.CLOSED)
                self.save(session_id, question)
                await self.refresh(session_id, [question], thread=thread)

    async def outcome(self, session_id: str, key: str, state: QuestionState) -> None:
        question = self.record(session_id, key)
        if question is not None and question.state != QuestionState.ANSWERED:
            question = replace(question, state=state)
            self.save(session_id, question)
            await self.refresh(session_id, [question])

    async def refresh(self, session_id: str, questions: list[StoredQuestion], *, thread: discord.Thread | None = None) -> None:
        async def edit_cards() -> None:
            for question in questions:
                async with self.edit_locks.setdefault(question.key, asyncio.Lock()):
                    question = self.record(session_id, question.key) or question
                    channel = (
                        self.bridge.bot.get_channel(question.legacy_parent_id)
                        if question.legacy_parent_id is not None
                        else thread or self.bridge.thread_channel(question.thread_id)
                    )
                    if isinstance(channel, (discord.Thread, discord.TextChannel)) and question.message_id is not None:
                        try:
                            attachment = (
                                discord.File(
                                    BytesIO("\n\n".join(body for _, body in question.questions).encode()), filename="questions.md"
                                )
                                if question.has_attachment
                                and question.state != QuestionState.ANSWERED
                                and question.legacy_parent_id is None
                                else None
                            )
                            try:
                                await channel.get_partial_message(question.message_id).edit(
                                    view=self.view(session_id, question),
                                    allowed_mentions=agent_session_allowed_mentions(),
                                    attachments=[attachment] if attachment is not None else [],
                                )
                            finally:
                                if attachment is not None:
                                    attachment.close()
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
        if current is not None and current.legacy_parent_id is not None:
            if not self.cards.bridge.is_operator(interaction.user):
                await interaction.response.send_message("Only session operators can use this card.", ephemeral=True)
                return
            await self.cards.refresh(self.session_id, [current])
            await interaction.response.send_message("Continue in the session thread linked on this earlier card.", ephemeral=True)
            return
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
        self.add_item(discord.ui.TextDisplay(f"[Read questions and recommendations]({question.source_url})"))
        groups = list(question.questions)
        for number, body in groups:
            field = discord.ui.TextInput[QuestionAnswerModal](
                style=discord.TextStyle.paragraph, max_length=1800 // (2 * len(groups))
            )
            self.fields.append(field)
            self.add_item(discord.ui.Label(text=number[:45], description=body[:100] or None, component=field))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.cards.lock(self.question.key):
            question = self.cards.record(self.session_id, self.question.key)
            error = self.cards.context_error(interaction, self.session_id, question)
            if question is not None and (
                question.questions != self.question.questions
                or question.epoch != self.question.epoch
                or question.message_id != self.question.message_id
                or question.revision != self.question.revision
            ):
                error = "The question changed; reopen its Answer form."
            if error or interaction.user.id != self.user_id:
                await interaction.followup.send(error or "This form belongs to another operator.", ephemeral=True)
                return
            if any(not field.value.strip() for field in self.fields):
                await interaction.followup.send("Answer every question before submitting.", ephemeral=True)
                return
            text = "\n\n".join(
                f"{number}: {field.value.strip()}" for (number, _), field in zip(self.question.questions, self.fields, strict=True)
            )
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
                    error = await self.cards.bridge.controls.send_reply(session, thread, interaction.user, text, message.id)
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
                error or "Decision recorded. Watch your thread reply for native delivery confirmation.", ephemeral=True
            )
