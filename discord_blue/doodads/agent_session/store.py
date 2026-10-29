"""Session recovery hints, atomically persisted by one ordered writer task."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import os
import tempfile
import time
import weakref
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Literal, get_args, cast

from discord_blue.doodads.agent_session.sessions import CleanupStep

logger = logging.getLogger(__name__)
SessionState = Literal["attaching", "live", "grace", "closing", "closed"]


SESSION_STORE_WAIT_TIMEOUT_SECONDS = 1


class QuestionState(StrEnum):
    WAITING = "Waiting"
    UNCERTAIN = "Uncertain"
    DELIVERED = "Delivered"
    NOT_DELIVERED = "Not delivered"
    CLOSED = "Closed"
    ANSWERED = "Answered"
    # Answered, but sent with the rest of its group once every card from the same agent reply is answered.
    HELD = "Waiting to send"
    # Sent, but its acknowledgment can no longer arrive: the bot restarted or the conversation ended first.
    UNCONFIRMED = "Delivery not confirmed"


@dataclass(frozen=True, slots=True)
class StoredQuestion:
    key: str
    epoch: str
    thread_id: int
    source_url: str
    questions: tuple[tuple[str, str], ...]
    sources: tuple[tuple[int, str], ...] = ()
    message_id: int | None = None
    state: QuestionState = QuestionState.WAITING
    has_attachment: bool = False
    answer: str = ""
    decision_url: str = ""
    complete: bool = True
    legacy_parent_id: int | None = None
    revision: int = 0
    # Cards from one agent reply share a group; their answers go to the agent together.
    group: str = ""

    @classmethod
    def parse(cls, value: object) -> StoredQuestion:
        if not isinstance(value, dict):
            raise ValueError("invalid question record")
        fields = {key: field for key, field in value.items() if key != "parent_id"}
        if value.get("parent_id") not in {None, value.get("thread_id")}:
            fields["legacy_parent_id"] = value["parent_id"]
        question = cls(**fields)
        if (
            not all(
                isinstance(v, str)
                for v in (
                    question.key,
                    question.epoch,
                    question.source_url,
                    question.state,
                    question.answer,
                    question.decision_url,
                    question.group,
                )
            )
            or type(question.revision) is not int
            or question.revision < 0
            or type(question.complete) is not bool
            or (question.legacy_parent_id is not None and type(question.legacy_parent_id) is not int)
            or type(question.thread_id) is not int
            or (question.message_id is not None and type(question.message_id) is not int)
            or not isinstance(question.questions, (list, tuple))
            or any(
                not isinstance(q, (list, tuple)) or len(q) != 2 or not all(isinstance(v, str) for v in q) for q in question.questions
            )
        ):
            raise ValueError("invalid question fields")
        return dataclasses.replace(
            question,
            questions=tuple((number, body) for number, body in question.questions),
            sources=tuple((int(message_id), str(signature)) for message_id, signature in question.sources),
            state=QuestionState(question.state),
        )


@dataclass(frozen=True, slots=True)
class StoredSession:
    thread_id: int
    notification_id: int | None
    marker: str
    status: SessionState
    grace_until: float
    updated_at: float
    pending_steps: tuple[CleanupStep, ...] | None = None
    recovery_attempts: int = 0
    control_message_id: int | None = None
    questions: tuple[StoredQuestion, ...] = ()

    @classmethod
    def parse(cls, value: object) -> StoredSession:
        if not isinstance(value, dict):
            raise ValueError("record is not an object")
        thread, notification = value.get("thread_id"), value.get("notification_id")
        control = value.get("control_message_id")
        if control is not None and (type(control) is not int or control <= 0):
            raise ValueError("invalid status anchor")
        marker, status = value.get("marker"), value.get("status")
        grace, updated = value.get("grace_until"), value.get("updated_at")
        if (
            type(thread) is not int
            or thread <= 0
            or (notification is not None and (type(notification) is not int or notification <= 0))
        ):
            raise ValueError("invalid Discord IDs")
        if not isinstance(marker, str) or status not in {"attaching", "live", "grace", "closing", "closed"}:
            raise ValueError("invalid identity or state")
        if not isinstance(grace, (int, float)) or not isinstance(updated, (int, float)):
            raise ValueError("invalid timestamps")
        if not math.isfinite(grace) or not math.isfinite(updated):
            raise ValueError("non-finite timestamps")
        steps = value.get("pending_steps")
        attempts = value.get("recovery_attempts", 0)
        if type(attempts) is not int or attempts < 0:
            raise ValueError("invalid recovery attempts")
        if steps is not None and (not isinstance(steps, list) or any(step not in get_args(CleanupStep) for step in steps)):
            raise ValueError("invalid cleanup steps")
        questions = value.get("question_cards", value.get("questions", []))
        if not isinstance(questions, (list, tuple)):
            raise ValueError("invalid questions")
        return cls(
            thread,
            notification,
            marker,
            status,
            float(grace),
            float(updated),
            cast(tuple[CleanupStep, ...], tuple(steps)) if steps is not None else None,
            attempts,
            control,
            tuple(StoredQuestion.parse(q) for q in questions),
        )

    def serialize(self) -> dict[str, object]:
        value = asdict(self)
        value["question_cards"] = value.pop("questions")
        return value


class SessionStore:
    _instances: ClassVar[weakref.WeakValueDictionary[Path, SessionStore]] = weakref.WeakValueDictionary()

    @classmethod
    def for_path(cls, path: Path) -> SessionStore:
        path = path.resolve()
        store = cls._instances.get(path)
        if store is None:
            store = cls(path)
            cls._instances[path] = store
        return store

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: dict[str, StoredSession] = {}
        self.error: OSError | None = None
        self._queue: asyncio.Queue[dict[str, StoredSession] | asyncio.Future[None] | None] = asyncio.Queue()
        self._writer: asyncio.Task[None] | None = None
        self._closing = False
        self._lifecycle = asyncio.Lock()
        self._queued_snapshot: dict[str, StoredSession] | None = None
        self._writing_since: float | None = None

    def unhealthy(self, stall_seconds: float) -> bool:
        return (
            self.error is not None
            or (self._writer is not None and self._writer.done() and not self._closing)
            or (self._writing_since is not None and time.monotonic() - self._writing_since > stall_seconds)
        )

    def enqueue_snapshot(self) -> None:
        if self._queued_snapshot is None:
            self._queued_snapshot = dict(self.records)
            self._queue.put_nowait(self._queued_snapshot)
        else:
            self._queued_snapshot.clear()
            self._queued_snapshot.update(self.records)

    async def start(self) -> None:
        async with self._lifecycle:
            if self._writer is not None:
                return
            self.records = await asyncio.to_thread(self._read)
            self._closing = False
            self._writer = asyncio.create_task(self._write_loop(), name="agent-session-store-writer")

    def _read(self) -> dict[str, StoredSession]:
        try:
            value = json.loads(self.path.read_text())
            if not isinstance(value, dict):
                raise ValueError("store is not an object")
            return {key: StoredSession.parse(record) for key, record in value.items()}
        except FileNotFoundError:
            logger.info("Agent session store is missing; using Discord discovery")
        except (OSError, ValueError, TypeError):
            logger.warning("Unable to load Agent session store; using Discord discovery", exc_info=True)
        return {}

    @property
    def accepts_writes(self) -> bool:
        return self._writer is not None and not self._closing

    def put(self, session_id: str, record: StoredSession) -> None:
        if not self.accepts_writes:
            return  # Bridges served directly by transport tests have no persistent lifecycle.
        self.records[session_id] = record
        self.enqueue_snapshot()

    def forget(self, session_id: str) -> None:
        if self._writer is not None and not self._closing and self.records.pop(session_id, None) is not None:
            self.enqueue_snapshot()

    def retry_failed_write(self) -> None:
        if self.error is not None and self._writer is not None and not self._closing:
            self.enqueue_snapshot()

    async def flush(self) -> None:
        if self._writer is None:
            return
        if self._closing or self._writer.done():
            await asyncio.shield(self._writer)
            return
        barrier = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(barrier)
        writer = self._writer
        await asyncio.wait({barrier, writer}, return_when=asyncio.FIRST_COMPLETED)
        if writer.done():
            writer.result()
        if self.error is not None:
            raise self.error

    async def close(self) -> None:
        async with self._lifecycle:
            if self._writer is None:
                return
            self._closing = True
            self._queue.put_nowait(None)
            try:
                await asyncio.shield(self._writer)
            finally:
                self._writer = None
                self._queue = asyncio.Queue()
                self._queued_snapshot = None

    async def _write_loop(self) -> None:
        while True:
            item = await self._queue.get()
            snapshot = None
            barriers = []
            stopping = False
            while True:
                if item is None:
                    stopping = True
                    break
                if isinstance(item, asyncio.Future):
                    barriers.append(item)
                else:
                    snapshot = item
                    if item is self._queued_snapshot:
                        self._queued_snapshot = None
                if self._queue.empty():
                    break
                item = self._queue.get_nowait()
            if snapshot is not None:
                self._writing_since = time.monotonic()
                try:
                    await asyncio.to_thread(self._write, snapshot)
                    self.error = None
                except OSError as exc:
                    self.error = exc
                    logger.exception("Unable to persist Agent session store")
                finally:
                    self._writing_since = None
            for barrier in barriers:
                if not barrier.done():
                    barrier.set_result(None)
            if stopping:
                return

    def _write(self, records: dict[str, StoredSession]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump({key: record.serialize() for key, record in records.items()}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
