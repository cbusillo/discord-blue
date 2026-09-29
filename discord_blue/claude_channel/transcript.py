"""Session titles Claude Code keeps in its transcript.

Claude Code appends `{"type":"ai-title","aiTitle":...}` records as it names a
session itself (the name the /resume picker shows), and
`{"type":"custom-title","customTitle":...}` when the user names it with `-n` or
`/rename`. Hooks give the transcript's path. Only a bounded tail of a regular
file is read, off the event loop and within a time bound, and the result is
cached until the file changes. A read that fails or takes too long gives no
titles.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from dataclasses import dataclass

TAIL_BYTES = 256 * 1024
READ_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class Titles:
    custom: str | None = None
    ai: str | None = None


class TranscriptTitles:
    def __init__(self) -> None:
        self.cached: tuple[tuple[str, int, int], Titles] | None = None

    async def read(self, path: str) -> Titles:
        """The transcript's titles; a stalled disk must not hold up hooks, Discord controls or heartbeats."""
        if not path.endswith(".jsonl"):
            return Titles()
        try:
            return await asyncio.wait_for(asyncio.to_thread(self.read_now, path), timeout=READ_TIMEOUT_SECONDS)
        except TimeoutError:
            return Titles()

    def read_now(self, path: str) -> Titles:
        try:
            status = os.stat(path)
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
        for line in lines:
            if b'"ai-title"' not in line and b'"custom-title"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") == "ai-title" and isinstance(record.get("aiTitle"), str):
                ai = record["aiTitle"]
            elif record.get("type") == "custom-title" and isinstance(record.get("customTitle"), str):
                custom = record["customTitle"]
        return Titles(custom=custom, ai=ai)
