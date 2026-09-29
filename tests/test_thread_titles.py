from __future__ import annotations

import asyncio
import dataclasses
import time
import unittest

from discord_blue.doodads.agent_session.renames import RENAME_WINDOW_SECONDS, ThreadRenamer
from discord_blue.doodads.agent_session.threads import (
    DISCORD_THREAD_NAME_LIMIT,
    HARNESS_ICONS,
    distinct_thread_name,
    session_thread_name,
)
from discord_blue.session_titles import LABEL_LIMIT, SessionLabel, substantial
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

    def test_sessions_that_would_share_a_name_are_told_apart(self) -> None:
        icon = HARNESS_ICONS["codex"]
        base = dataclasses.replace(make_hello(), cwd="/w/shiny-infra-ops", harness="codex", branch="main")
        first, second, third = (dataclasses.replace(base, session_id=f"01a0eb5e-{n}{n}{n}{n}") for n in (1, 2, 3))
        taken: set[str] = set()
        names = []
        for hello in (first, second, third):
            names.append(distinct_thread_name(hello, taken))
            taken.add(names[-1])
        self.assertEqual(names, [f"{icon} shiny-infra-ops", f"{icon} shiny-infra-ops · main", f"{icon} shiny-infra-ops #3333"])
        # A suffix survives truncation.
        long = dataclasses.replace(third, title="word " * 40)
        taken = {session_thread_name(long), distinct_thread_name(dataclasses.replace(long, branch=None), set())}
        suffixed = distinct_thread_name(dataclasses.replace(long, branch=None), taken)
        self.assertTrue(suffixed.endswith(" #3333"))
        self.assertLessEqual(utf16_length(suffixed), DISCORD_THREAD_NAME_LIMIT)

    def test_a_long_title_is_cut_to_discords_limit_with_the_icon_counted(self) -> None:
        for harness in HARNESS_ICONS:
            with self.subTest(harness):
                name = session_thread_name(dataclasses.replace(make_hello(), harness=harness, title="word " * 60))
                self.assertLessEqual(utf16_length(name), DISCORD_THREAD_NAME_LIMIT)
                self.assertTrue(name.startswith(HARNESS_ICONS[harness]) and name.endswith("…"))


class SessionLabelTests(unittest.TestCase):
    def test_low_information_prompts_and_tags_do_not_name_a_session(self) -> None:
        for prompt in (
            "Continue",
            "go",
            "ok thanks",
            "Yes, go ahead. Proceed!",
            "/clear",
            '<pasted_content id="fc27">',
            "<system-reminder>You must follow these rules carefully</system-reminder> ok",
            "<command-name>/model</command-name><command-args>haiku please now</command-args>",
            "   ",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(substantial(prompt))

    def test_labels_are_short_plain_and_cut_at_a_word_boundary(self) -> None:
        cases = {
            "Fix the flaky\n  login test": "Fix the flaky login test",
            '<pasted_content id="fc27">log</pasted_content> Summarize this "incident" report for me': (
                "Summarize this incident report for me"
            ),
            "Read https://example.com/very/long/path and fix the failing deploy": "Read and fix the failing deploy",
            "I notice it just says codex-lab and the Codex session shows the repo and continue": (
                "I notice it just says codex-lab and the Codex"
            ),
        }
        for prompt, label in cases.items():
            with self.subTest(prompt=prompt):
                shown = substantial(prompt)
                self.assertEqual(shown, label)
                assert shown is not None
                self.assertLessEqual(len(shown), LABEL_LIMIT)
                self.assertFalse(shown.endswith("…"))

    def test_tag_stripping_takes_linear_time_on_a_huge_paste(self) -> None:
        pastes = {
            "void tags": "<br>" * 1_000_000 + " Fix the flaky login test",
            "unclosed tags": "<div><span>" * 500_000 + " Fix the flaky login test",
            # The reviewer's input: one unterminated tag whose name keeps going.
            "unterminated hyphenated tag": "<" + "a-" * 32768,
        }
        for case, paste in pastes.items():
            with self.subTest(case):
                started = time.process_time()
                substantial(paste)
                # A rescan per unmatched tag took seconds on 64 KB; one bounded pass takes milliseconds.
                self.assertLess(time.process_time() - started, 0.5)

    def test_an_unclosed_system_wrapper_hides_the_rest_but_other_tags_do_not(self) -> None:
        self.assertIsNone(substantial("<system-reminder>Follow these rules before you answer the user"))
        # Pasted content never labels a session, even when its closing tag is missing.
        self.assertIsNone(substantial('<pasted_content id="fc27"> Summarize this incident report'))
        self.assertEqual(substantial("Fix the <b>bold</b> header <br> spacing today please"), "Fix the header spacing today please")

    def test_a_credential_in_an_oversized_paste_never_reaches_the_label(self) -> None:
        # The paste outgrows the bounded input, so its closing tag is cut off.
        prompt = '<pasted_content id="p1">DB_PASSWORD=hunter2-prod-secret ' + "x " * 5000 + "</pasted_content> Fix login"
        self.assertIsNone(substantial(prompt))
        self.assertIsNone(SessionLabel(prompt=prompt).current)

    def test_precedence_is_name_then_auto_title_then_first_substantial_prompt(self) -> None:
        label = SessionLabel(name="Continue", prompt="Continue")
        # Codex names a thread "Continue" by itself; that is not a name.
        self.assertIsNone(label.current)
        self.assertEqual(label.update(prompt="Fix the flaky login test"), "Fix the flaky login test")
        self.assertIsNone(label.update(prompt="Now update the release notes"))
        self.assertEqual(label.update(auto="Login flake investigation"), "Login flake investigation")
        self.assertEqual(label.update(name="auth-refactor"), "auth-refactor")
        self.assertIsNone(label.update(auto="Something newer"))
        # Clearing the name falls back to the auto title.
        self.assertEqual(label.update(clear_name=True), "Something newer")


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
