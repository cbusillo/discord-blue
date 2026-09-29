"""Session titles Claude Code keeps in its transcript.

Claude Code appends `{"type":"ai-title","aiTitle":...}` records as it names a
session itself (the name the /resume picker shows), and
`{"type":"custom-title","customTitle":...}` when the user names it with `-n` or
`/rename`. Hooks give the transcript's path. Only a bounded tail is read, and
the result is cached until the file changes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

TAIL_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class Titles:
    custom: str | None = None
    ai: str | None = None


class TranscriptTitles:
    def __init__(self) -> None:
        self.cached: tuple[tuple[str, int, int], Titles] | None = None

    def read(self, path: str) -> Titles:
        if not path.endswith(".jsonl"):
            return Titles()
        try:
            status = os.stat(path)
        except OSError:
            return Titles()
        key = (path, status.st_size, status.st_mtime_ns)
        if self.cached is not None and self.cached[0] == key:
            return self.cached[1]
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
