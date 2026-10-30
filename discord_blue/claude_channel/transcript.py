"""Session titles Claude Code keeps in its transcript.

Claude Code appends `{"type":"ai-title","aiTitle":...}` records as it names a
session itself (the name the /resume picker shows), and
`{"type":"custom-title","customTitle":...}` when the user names it with `-n` or
`/rename`. Hooks give the transcript's path. Only a bounded tail of a regular
file is read, off the event loop and within a time bound, and the result is
cached until the file changes. Reads run on this reader's own single worker,
never the event loop's shared executor, and at most one is in flight: a read
stalled on slow storage ties up only that worker, and meanwhile the last titles
read are used.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

TAIL_BYTES = 256 * 1024
# Hook output, such as a failing test report, can be large; read whole records around a stop.
STOP_READ_LIMIT = 16 * 1024 * 1024
READ_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class Titles:
    custom: str | None = None
    ai: str | None = None
    before_first_prompt: bool = False
    scheduled_prompt_ids: frozenset[str] = frozenset()


class TranscriptTitles:
    def __init__(self) -> None:
        self.cached: tuple[tuple[str, int, int], Titles] | None = None
        self.latest = Titles()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dui-transcript")
        self.in_flight: asyncio.Future[Titles] | None = None

    async def read(self, path: str) -> Titles:
        """The transcript's titles; a stalled disk must not hold up hooks, Discord controls or heartbeats."""
        if not path.endswith(".jsonl"):
            return Titles()
        if self.in_flight is not None and not self.in_flight.done():
            return self.latest  # The previous read is still stuck; do not queue another behind it.
        self.in_flight = asyncio.get_running_loop().run_in_executor(self.executor, self.read_now, path)
        try:
            # shield: a timeout abandons the wait, not the read, which finishes on the worker in its own time.
            self.latest = await asyncio.wait_for(asyncio.shield(self.in_flight), timeout=READ_TIMEOUT_SECONDS)
        except TimeoutError:
            return self.latest
        return self.latest

    def read_now(self, path: str) -> Titles:
        try:
            status = os.stat(path)
        except FileNotFoundError:
            return Titles(before_first_prompt=True)
        except OSError:
            return Titles()
        if not stat.S_ISREG(status.st_mode):
            return Titles()
        key = (path, status.st_size, status.st_mtime_ns)
        cached = self.cached
        if cached is not None and cached[0] == key:
            return cached[1]
        titles = self.scan(path, status.st_size)
        self.cached = (key, titles)
        return titles

    async def size(self, path: str) -> int | None:
        """Where the transcript ends now: records written later answer for what happened after this point."""
        return await self.bounded(transcript_size, path)

    async def stop_blocked(self, path: str, offset: int) -> bool | None:
        """Whether another Stop hook rejected the answer that ends at offset, sending Claude back to rewrite it."""
        return await self.bounded(stop_verdict, path, offset)

    async def tool_answered(self, path: str, offset: int, tool_use_id: str) -> bool | None:
        """Whether the tool call has a result written after offset: it ran, failed, or was rejected in the terminal."""
        return await self.bounded(tool_result_after, path, offset, tool_use_id)

    async def bounded[T](self, read: Callable[..., T | None], path: str, *args: object) -> T | None:
        if not path.endswith(".jsonl"):
            return None
        try:
            return await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(self.executor, read, path, *args), READ_TIMEOUT_SECONDS
            )
        except TimeoutError:
            return None

    @staticmethod
    def scan(path: str, size: int) -> Titles:
        start = max(0, size - TAIL_BYTES)
        try:
            with open(path, "rb") as transcript:
                transcript.seek(start)
                lines = transcript.read(TAIL_BYTES).split(b"\n")
        except OSError:
            return Titles()
        if start:
            lines = lines[1:]  # The first line was cut by the seek.
        custom = ai = None
        scheduled_prompt_ids: set[str] = set()
        before_first_prompt = not start
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                before_first_prompt = False
                continue
            if not isinstance(record, dict):
                before_first_prompt = False
                continue
            if (
                record.get("type") == "user"
                and record.get("isMeta") is True
                and record.get("promptSource") == "system"
                and record.get("turnOrigin") == "scheduled"
                and isinstance(prompt_id := record.get("promptId"), str)
                and prompt_id
            ):
                scheduled_prompt_ids.add(prompt_id)
            if record.get("type") in {"assistant", "user"}:
                before_first_prompt = False
            if record.get("type") == "ai-title" and isinstance(record.get("aiTitle"), str):
                ai = record["aiTitle"]
            elif record.get("type") == "custom-title" and isinstance(record.get("customTitle"), str):
                custom = record["customTitle"]
        return Titles(
            custom=custom, ai=ai, before_first_prompt=before_first_prompt, scheduled_prompt_ids=frozenset(scheduled_prompt_ids)
        )


def transcript_size(path: str) -> int | None:
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def stop_verdict(path: str, offset: int) -> bool | None:
    """Whether the Stop hooks that ran at offset sent Claude back to work; None until their summary is written.

    Claude Code writes each Stop invocation's summary only after every Stop hook returns, this session's own
    included, so the first summary after offset (the transcript's end when this session's hook ran) is this
    stop's. Its toolUseID ties it to the invocation's hook attachments, which may land on either side of
    offset: a blocking error or added context means Claude continues and this answer was not final.
    """
    after = read_records(path, offset, STOP_READ_LIMIT)
    summary = next((record for record in after if record.get("subtype") == "stop_hook_summary"), None)
    if summary is None or not isinstance(stop := summary.get("toolUseID"), str):
        return None
    start = max(0, offset - STOP_READ_LIMIT)
    # A hook that finished before this session's own may already be recorded before offset.
    before = read_records(path, start, offset - start, partial_first=start > 0)
    return any(continues(record, stop) for record in [*before, *after])


def tool_result_after(path: str, offset: int, tool_use_id: str) -> bool:
    for record in read_records(path, offset, STOP_READ_LIMIT):
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if record.get("type") == "user" and isinstance(content, list):
            if any(isinstance(part, dict) and part.get("tool_use_id") == tool_use_id for part in content):
                return True
    return False


def continues(record: dict[str, object], stop: str) -> bool:
    attachment = record.get("attachment")
    return (
        isinstance(attachment, dict)
        and attachment.get("toolUseID") == stop
        and attachment.get("hookEvent") == "Stop"
        and attachment.get("type") in {"hook_blocking_error", "hook_additional_context"}
    )


def read_records(path: str, start: int, length: int, *, partial_first: bool = False) -> list[dict[str, object]]:
    try:
        with open(path, "rb") as transcript:
            transcript.seek(start)
            lines = transcript.read(length).split(b"\n")
    except OSError:
        return []
    if partial_first:
        lines = lines[1:]  # The first line was cut by the seek.
    records = []
    for line in lines:
        try:
            record = json.loads(line) if line.strip() else None
        except ValueError:
            continue  # Includes a last line still being written.
        if isinstance(record, dict):
            records.append(record)
    return records
