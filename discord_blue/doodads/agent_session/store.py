"""Session recovery hints, atomically persisted by one ordered writer task."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, get_args, cast

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
        )


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: dict[str, StoredSession] = {}
        self.error: OSError | None = None
        self._queue: asyncio.Queue[dict[str, StoredSession] | asyncio.Future[None] | None] = asyncio.Queue()
        self._writer: asyncio.Task[None] | None = None
        self._closing = False

    async def start(self) -> None:
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
        self._queue.put_nowait(dict(self.records))

    def forget(self, session_id: str) -> None:
        if self._writer is not None and not self._closing and self.records.pop(session_id, None) is not None:
            self._queue.put_nowait(dict(self.records))

    def retry_failed_write(self) -> None:
        if self.error is not None and self._writer is not None and not self._closing:
            self._queue.put_nowait(dict(self.records))

    async def flush(self) -> None:
        if self._writer is None:
            return
        if self._closing or self._writer.done():
            await asyncio.shield(self._writer)
            return
        barrier = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(barrier)
        await asyncio.shield(barrier)
        if self.error is not None:
            raise self.error

    async def close(self) -> None:
        if self._writer is None:
            return
        if not self._closing:
            self._closing = True
            self._queue.put_nowait(None)
        await asyncio.shield(self._writer)
        self._writer = None

    async def _write_loop(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, asyncio.Future):
                if not item.done():
                    item.set_result(None)
                continue
            try:
                await asyncio.to_thread(self._write, item)
                self.error = None
            except OSError as exc:
                self.error = exc
                logger.exception("Unable to persist Agent session store")

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
