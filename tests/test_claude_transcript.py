from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from discord_blue.claude_channel import transcript as transcript_module
from discord_blue.claude_channel.transcript import TAIL_BYTES, Titles, TranscriptTitles


def record(kind: str, **fields: str) -> str:
    return json.dumps({"type": kind, **fields, "sessionId": "s"}) + "\n"


class TranscriptTitlesTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_latest_titles_are_read_from_a_bounded_tail_and_cached_until_the_file_changes(self) -> None:
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
            self.assertEqual(await titles.read(str(path)), Titles(custom=None, ai="Phone offline for host"))

            with patch.object(TranscriptTitles, "scan", side_effect=AssertionError("rescanned")):
                self.assertEqual((await titles.read(str(path))).ai, "Phone offline for host")
            with path.open("a") as lines:
                lines.write(record("ai-title", aiTitle="Newer title"))
            self.assertEqual((await titles.read(str(path))).ai, "Newer title")

    async def test_missing_foreign_and_non_regular_paths_give_no_titles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "session.jsonl"
            os.mkfifo(fifo)  # Opening it would block; it must be refused without opening.
            titles = TranscriptTitles()
            for path in ("", "/nonexistent/session.jsonl", "/etc/passwd", str(fifo)):
                with self.subTest(path=path):
                    self.assertEqual(await titles.read(path), Titles())

    async def test_a_read_that_stalls_gives_no_titles_without_blocking_the_event_loop(self) -> None:
        release = threading.Event()

        def stalled(_self: TranscriptTitles, _path: str) -> Titles:
            release.wait(5)  # A transcript on stalled storage.
            return Titles(ai="too late")

        with (
            patch.object(transcript_module, "READ_TIMEOUT_SECONDS", 0.05),
            patch.object(TranscriptTitles, "read_now", stalled),
        ):
            self.assertEqual(await TranscriptTitles().read("/w/session.jsonl"), Titles())
        release.set()

    async def test_repeated_stalled_reads_use_one_worker_and_leave_the_shared_executor_free(self) -> None:
        release = threading.Event()
        started = threading.Semaphore(0)

        def stalled(_self: TranscriptTitles, _path: str) -> Titles:
            started.release()
            release.wait(5)  # Storage that stopped answering.
            return Titles(ai="late")

        titles = TranscriptTitles()
        titles.latest = Titles(ai="Known title")
        with (
            patch.object(transcript_module, "READ_TIMEOUT_SECONDS", 0.02),
            patch.object(TranscriptTitles, "read_now", stalled),
        ):
            answers = [await titles.read("/w/session.jsonl") for _ in range(20)]
            # Other work on the loop's shared executor, such as DNS lookups, still runs at once.
            shared = await asyncio.wait_for(asyncio.to_thread(lambda: "shared executor free"), timeout=1)
            workers = len(titles.executor._threads)
        release.set()

        self.assertEqual(set(answers), {Titles(ai="Known title")})
        self.assertEqual((shared, workers), ("shared executor free", 1))
        # Only the first read started; the rest reused the last titles instead of queueing.
        self.assertTrue(started.acquire(timeout=1))
        self.assertFalse(started.acquire(timeout=0.1))
