"""Session recovery hints, atomically persisted by one ordered writer task."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
import time
import weakref
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar, Literal, get_args, cast

from discord_blue.doodads.agent_session.sessions import CleanupStep

logger = logging.getLogger(__name__)
SessionState = Literal["attaching", "live", "grace", "closing", "closed"]


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

    @classmethod
    def parse(cls, value: object) -> StoredSession:
        if not isinstance(value, dict):
            raise ValueError("record is not an object")
        thread, notification = value.get("thread_id"), value.get("notification_id")
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
        return cls(
            thread,
            notification,
            marker,
            status,
            float(grace),
            float(updated),
            cast(tuple[CleanupStep, ...], tuple(steps)) if steps is not None else None,
            attempts,
        )


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

    def put(self, session_id: str, record: StoredSession) -> None:
        if self._writer is None or self._closing:
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
                json.dump({key: asdict(record) for key, record in records.items()}, output)
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
