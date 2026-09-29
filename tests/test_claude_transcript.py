from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from discord_blue.claude_channel.transcript import TAIL_BYTES, Titles, TranscriptTitles


def record(kind: str, **fields: str) -> str:
    return json.dumps({"type": kind, **fields, "sessionId": "s"}) + "\n"


class TranscriptTitlesTests(unittest.TestCase):
    def test_the_latest_titles_are_read_from_a_bounded_tail_and_cached_until_the_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            filler = json.dumps({"type": "user", "message": "x" * 1000}) + "\n"
            # A title beyond the tail is not read; the reader never scans the whole transcript.
            path.write_text(record("custom-title", customTitle="Beyond the tail") + filler * (TAIL_BYTES // len(filler) + 2))
            with path.open("a") as lines:
                lines.write(record("ai-title", aiTitle="First guess"))
                lines.write(filler)
                lines.write(record("ai-title", aiTitle="Phone offline for host"))
            titles = TranscriptTitles()
            self.assertEqual(titles.read(str(path)), Titles(custom=None, ai="Phone offline for host"))

            with patch.object(TranscriptTitles, "scan", side_effect=AssertionError("rescanned")):
                self.assertEqual(titles.read(str(path)).ai, "Phone offline for host")
            with path.open("a") as lines:
                lines.write(record("ai-title", aiTitle="Newer title"))
            self.assertEqual(titles.read(str(path)).ai, "Newer title")

    def test_a_missing_or_foreign_path_gives_no_titles(self) -> None:
        titles = TranscriptTitles()
        for path in ("", "/nonexistent/session.jsonl", "/etc/passwd"):
            with self.subTest(path=path):
                self.assertEqual(titles.read(path), Titles())
