from __future__ import annotations

import asyncio
import dataclasses
import time
import unittest

from discord_blue.doodads.agent_session.renames import RENAME_WINDOW_SECONDS, ThreadRenamer
from discord_blue.doodads.agent_session.threads import DISCORD_THREAD_NAME_LIMIT, HARNESS_ICONS, session_thread_name
from discord_blue.session_titles import SessionLabel, substantial
from tests.fakes_agent_session import make_hello


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class ThreadNameTests(unittest.TestCase):
    def test_icon_repo_and_label_in_order_of_precedence(self) -> None:
        claude, codex = HARNESS_ICONS["claude"], HARNESS_ICONS["codex"]
        base = dataclasses.replace(make_hello(), cwd="/Users/me/Developer/codex-lab", branch="main")
        cases = {
            "Claude with a label": (
                dataclasses.replace(base, harness="claude", title="Fix the login bug"),
                f"{claude} codex-lab · Fix the login bug",
            ),
            "Codex falls back to a feature branch": (
                dataclasses.replace(base, harness="codex", branch="fix/login"),
                f"{codex} codex-lab · fix/login",
            ),
            "nothing but the repo on a default branch": (dataclasses.replace(base, harness="claude"), f"{claude} codex-lab"),
            "a client that sends no harness": (dataclasses.replace(base, title="Deploy"), "codex-lab · Deploy"),
            "an unknown harness": (dataclasses.replace(base, harness="other", title="Deploy"), "codex-lab · Deploy"),
            "a title with line breaks": (
                dataclasses.replace(base, harness="codex", title="Fix\nthe  bug"),
                f"{codex} codex-lab · Fix the bug",
            ),
        }
        for case, (hello, name) in cases.items():
            with self.subTest(case):
                self.assertEqual(session_thread_name(hello), name)

    def test_a_huge_title_is_cut_without_scanning_all_of_it(self) -> None:
        hello = dataclasses.replace(make_hello(), harness="claude", title="x" * 5_000_000)
        started = time.process_time()
        name = session_thread_name(hello)
        # Scanning five million characters repeatedly would take minutes; bounded work takes microseconds.
        self.assertLess(time.process_time() - started, 0.5)
        self.assertEqual(utf16_length(name), DISCORD_THREAD_NAME_LIMIT)

    def test_a_long_title_is_cut_to_discords_limit_with_the_icon_counted(self) -> None:
        for harness in HARNESS_ICONS:
            with self.subTest(harness):
                name = session_thread_name(dataclasses.replace(make_hello(), harness=harness, title="word " * 60))
                self.assertLessEqual(utf16_length(name), DISCORD_THREAD_NAME_LIMIT)
                self.assertTrue(name.startswith(HARNESS_ICONS[harness]) and name.endswith("…"))


class SessionLabelTests(unittest.TestCase):
    def test_low_information_prompts_do_not_name_a_session(self) -> None:
        for prompt in (
            "Continue",
            "go",
            "yes",
            "ok thanks",
            "keep going",
            "Yes, go ahead. Proceed!",
            "/clear",
            "<command-name>x y z</command-name>",
            "   ",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(substantial(prompt))
        self.assertEqual(substantial("Fix the flaky\n  login test"), "Fix the flaky login test")

    def test_a_name_wins_the_latest_substantial_prompt_follows_and_unchanged_labels_are_not_resent(self) -> None:
        label = SessionLabel(name=None, prompt="Continue")
        self.assertIsNone(label.current)
        self.assertEqual(label.update(prompt="Fix the flaky login test"), "Fix the flaky login test")
        self.assertIsNone(label.update(prompt="continue"))
        self.assertEqual(label.update(prompt="Now update the release notes"), "Now update the release notes")
        self.assertEqual(label.update(name="auth-refactor"), "auth-refactor")
        self.assertIsNone(label.update(prompt="And one more substantial prompt"))
        # Clearing the name falls back to the latest substantial prompt.
        self.assertEqual(label.update(clear_name=True), "And one more substantial prompt")


class FakeThread:
    def __init__(self, name: str, *, cache_updates: bool = True) -> None:
        self.name = name
        self.edits: list[str] = []
        self.hang = False
        # discord.py returns the edited thread; the cached object's name updates only when the gateway says so.
        self.cache_updates = cache_updates

    async def edit(self, *, name: str) -> None:
        if self.hang:
            self.hang = False
            await asyncio.sleep(3600)  # discord.py sleeping through a rate limit
        self.edits.append(name)
        if self.cache_updates:
            self.name = name


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []
        self.gate = asyncio.Event()

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        await self.gate.wait()  # The test decides when the window has passed.
        self.gate.clear()
        self.now += seconds


class ThreadRenamerTests(unittest.IsolatedAsyncioTestCase):
    async def settle(self, _renamer: ThreadRenamer) -> None:
        for _ in range(20):
            await asyncio.sleep(0)

    async def test_renames_are_coalesced_deferred_past_the_rate_limit_and_skipped_when_unchanged(self) -> None:
        thread, clock = FakeThread("old"), FakeClock()
        renamer = ThreadRenamer(lambda _thread_id, _epoch: thread, clock=clock, sleep=clock.sleep)
        for name in ("a", "b", "c"):
            renamer.request(1, name, "epoch-1")  # Requests before the task runs keep only the latest.
        await self.settle(renamer)
        renamer.request(1, "d", "epoch-1")
        await self.settle(renamer)
        renamer.request(1, "d", "epoch-1")  # Already the thread's name: nothing to send.
        await self.settle(renamer)
        self.assertEqual(thread.edits, ["c", "d"])

        for name in ("e", "f"):
            renamer.request(1, name, "epoch-1")
        await self.settle(renamer)
        # The window allows RENAMES_PER_WINDOW renames, so the latest waits for it to reopen.
        self.assertEqual((thread.edits, clock.waits), (["c", "d"], [RENAME_WINDOW_SECONDS]))
        clock.gate.set()
        await self.settle(renamer)
        self.assertEqual(thread.edits, ["c", "d", "f"])
        await renamer.close()

    async def test_a_rename_discord_holds_back_is_retried_after_the_window(self) -> None:
        thread, clock = FakeThread("old"), FakeClock()
        renamer = ThreadRenamer(lambda _thread_id, _epoch: thread, clock=clock, sleep=clock.sleep, timeout=0.01)
        thread.hang = True
        renamer.request(1, "new", "epoch-1")
        await asyncio.sleep(0.05)
        self.assertEqual((thread.edits, clock.waits), ([], [RENAME_WINDOW_SECONDS]))
        clock.gate.set()
        await self.settle(renamer)
        self.assertEqual(thread.edits, ["new"])
        await renamer.close()

    async def test_a_thread_without_a_live_session_is_not_renamed(self) -> None:
        renamer = ThreadRenamer(lambda _thread_id, _epoch: None)
        renamer.request(1, "new", "epoch-1")
        await self.settle(renamer)
        self.assertEqual(renamer.tasks, {})

    async def test_a_rename_asked_for_by_an_earlier_session_epoch_is_dropped(self) -> None:
        thread, clock = FakeThread("old"), FakeClock()
        owner = {"epoch": "epoch-1"}
        renamer = ThreadRenamer(
            lambda _thread_id, epoch: thread if epoch == owner["epoch"] else None, clock=clock, sleep=clock.sleep
        )
        for name in ("a", "b", "c"):
            renamer.request(1, name, "epoch-1")
            await self.settle(renamer)
        self.assertEqual((thread.edits, clock.waits), (["a", "b"], [RENAME_WINDOW_SECONDS]))
        # The session reconnects under a new epoch while "c" waits for the window.
        owner["epoch"] = "epoch-2"
        clock.gate.set()
        await self.settle(renamer)
        self.assertEqual(thread.edits, ["a", "b"])
        await renamer.close()

    async def test_the_last_applied_name_decides_whether_a_rename_is_needed(self) -> None:
        thread, clock = FakeThread("A", cache_updates=False), FakeClock()
        renamer = ThreadRenamer(lambda _thread_id, _epoch: thread, clock=clock, sleep=clock.sleep)
        renamer.request(1, "B", "epoch-1")
        await self.settle(renamer)
        # The cache still says A, but Discord shows B; asking for A again must rename.
        renamer.request(1, "A", "epoch-1")
        await self.settle(renamer)
        self.assertEqual(thread.edits, ["B", "A"])
        await renamer.close()

    async def test_idle_rename_history_is_forgotten_and_close_clears_everything(self) -> None:
        clock = FakeClock()
        threads = {1: FakeThread("one"), 2: FakeThread("two")}
        renamer = ThreadRenamer(lambda thread_id, _epoch: threads[thread_id], clock=clock, sleep=clock.sleep)
        renamer.request(1, "one renamed", "epoch-1")
        await self.settle(renamer)
        clock.now += RENAME_WINDOW_SECONDS
        renamer.request(2, "two renamed", "epoch-1")
        await self.settle(renamer)
        self.assertEqual((set(renamer.recent), set(renamer.applied)), ({2}, {2}))
        await renamer.close()
        self.assertEqual((renamer.recent, renamer.applied, renamer.wanted, renamer.tasks), ({}, {}, {}, {}))
