from __future__ import annotations

import asyncio
import dataclasses
import time
import unittest
from collections.abc import Callable

import discord

from discord_blue.doodads.agent_session.thread_worker import RENAME_WINDOW_SECONDS, ThreadWorkers
from discord_blue.doodads.agent_session.threads import (
    DISCORD_THREAD_NAME_LIMIT,
    HARNESS_ICONS,
    distinct_thread_name,
    session_thread_name,
)
from discord_blue.session_titles import INPUT_LIMIT, LABEL_LIMIT, SessionLabel, substantial, typed_prompt
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


# Prompts Claude Code injects itself, as its UserPromptSubmit hook reports them.
HARNESS_PROMPTS = {
    "task-notification": (
        "<task-notification>\n<task-id>b7x</task-id>\n<status>completed</status>\n"
        "<summary>Background command finished with the full test run</summary>\n</task-notification>"
    ),
    "agent-message": '<agent-message from="a5df">\n[Subagent hand-back] The review found four medium issues\n</agent-message>',
    "system-reminder": "  <system-reminder>\nThe user has changed the working directory for this session\n</system-reminder>",
    "local-command-stdout": "<local-command-stdout>Compacted the conversation history for you</local-command-stdout>",
    "local-command-caveat": (
        "<local-command-caveat>Caveat: the messages below were generated by the user while running local "
        "commands.</local-command-caveat>"
    ),
    "unclosed": "<task-notification>\n" + "Background output that keeps going " * (INPUT_LIMIT // 20),
}


class TypedPromptTests(unittest.TestCase):
    def test_prompts_claude_code_injects_are_not_typed_prompts_or_labels(self) -> None:
        for kind, prompt in HARNESS_PROMPTS.items():
            with self.subTest(kind=kind):
                self.assertIsNone(typed_prompt(prompt))
                self.assertIsNone(SessionLabel().update(prompt=prompt))

    def test_a_slash_command_becomes_its_name_and_arguments(self) -> None:
        prompt = (
            "<command-message>review is running…</command-message>\n<command-name>/review</command-name>\n"
            "<command-args>152   high</command-args>"
        )

        self.assertEqual(typed_prompt(prompt), "/review 152 high")
        self.assertEqual(typed_prompt("<command-name>/clear</command-name>"), "/clear")
        self.assertIsNone(SessionLabel().update(prompt=prompt))

    def test_typed_prompts_pass_through_unchanged(self) -> None:
        for prompt in ("Fix the login bug", "<b>bold</b> is how HTML marks it", "Explain <task-notification> tags"):
            with self.subTest(prompt=prompt):
                self.assertEqual(typed_prompt(prompt), prompt)

    def test_filtering_a_huge_prompt_takes_linear_time(self) -> None:
        started = time.perf_counter()
        for prompt in ("<agent-message " + "<a" * 500_000, "<command-name>" + "<x>" * 500_000, "<" * 1_000_000):
            typed_prompt(prompt)
        self.assertLess(time.perf_counter() - started, 1.0)


class FakeThread:
    def __init__(self, name: str, *, cache_updates: bool = True) -> None:
        self.name = name
        self.edits: list[str] = []
        self.rate_limited_for = 0.0
        # discord.py returns the edited thread; the cached object's name updates only when the gateway says so.
        self.cache_updates = cache_updates

    async def edit(self, *, name: str) -> None:
        if self.rate_limited_for:
            retry_after, self.rate_limited_for = self.rate_limited_for, 0.0
            raise discord.RateLimited(retry_after)  # Longer than discord.py will sleep through (max_ratelimit_timeout).
        self.edits.append(name)
        if self.cache_updates:
            self.name = name


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RenameOnlyHooks:
    """The bridge side of the thread workers, for renames only."""

    def __init__(self, resolve: Callable[[int, str], FakeThread | None]) -> None:
        self.resolve = resolve

    def owned(self, _thread_id: int) -> bool:
        return True

    def rename_target(self, thread_id: int, epoch: str) -> FakeThread | None:
        return self.resolve(thread_id, epoch)

    async def post_close_notice(self, _thread: discord.Thread) -> None:
        raise AssertionError("renames only")

    def bot_user_id(self) -> int | None:
        return None

    async def add_configured_members(self, _thread: discord.Thread) -> None:
        raise AssertionError("renames only")


class ThreadRenameTests(unittest.IsolatedAsyncioTestCase):
    def workers(self, resolve: Callable[[int, str], FakeThread | None], clock: FakeClock) -> ThreadWorkers:
        workers = ThreadWorkers(RenameOnlyHooks(resolve), clock=clock)
        self.addCleanup(workers.stop)
        return workers

    @staticmethod
    async def settle() -> None:
        for _ in range(20):
            await asyncio.sleep(0)

    @staticmethod
    async def pass_time(workers: ThreadWorkers, clock: FakeClock, seconds: float) -> None:
        clock.now += seconds
        for worker in workers.workers.values():
            worker.start()  # Wakes a worker waiting out a window on the real clock.
        await ThreadRenameTests.settle()

    async def test_renames_are_coalesced_deferred_past_the_rate_limit_and_skipped_when_unchanged(self) -> None:
        thread, clock = FakeThread("old"), FakeClock()
        workers = self.workers(lambda _thread_id, _epoch: thread, clock)
        for name in ("a", "b", "c"):
            workers.rename(1, name, "epoch-1")  # Requests before the worker runs keep only the latest.
        await self.settle()
        workers.rename(1, "d", "epoch-1")
        await self.settle()
        workers.rename(1, "d", "epoch-1")  # Already the thread's name: nothing to send.
        await self.settle()
        self.assertEqual(thread.edits, ["c", "d"])

        for name in ("e", "f"):
            workers.rename(1, name, "epoch-1")
        await self.settle()
        # The window allows RENAMES_PER_WINDOW renames, so the latest waits for it to reopen.
        self.assertEqual(thread.edits, ["c", "d"])
        self.assertEqual(workers.worker(1).rename_delay(), RENAME_WINDOW_SECONDS)
        await self.pass_time(workers, clock, RENAME_WINDOW_SECONDS)
        self.assertEqual(thread.edits, ["c", "d", "f"])

    async def test_a_rename_discord_rate_limits_is_retried_when_discord_allows(self) -> None:
        thread, clock = FakeThread("old"), FakeClock()
        workers = self.workers(lambda _thread_id, _epoch: thread, clock)
        thread.rate_limited_for = 45.0
        workers.rename(1, "new", "epoch-1")
        await self.settle()
        self.assertEqual(thread.edits, [])
        await self.pass_time(workers, clock, 44.0)
        self.assertEqual(thread.edits, [])
        await self.pass_time(workers, clock, 1.0)
        self.assertEqual(thread.edits, ["new"])

    async def test_a_rate_limited_rename_back_to_the_cached_name_is_still_retried(self) -> None:
        thread, clock = FakeThread("A", cache_updates=False), FakeClock()
        workers = self.workers(lambda _thread_id, _epoch: thread, clock)
        workers.rename(1, "B", "epoch-1")
        await self.settle()
        # Discord shows B, the cache still says A; the rename back to A is rate limited.
        thread.rate_limited_for = 45.0
        workers.rename(1, "A", "epoch-1")
        await self.settle()
        await self.pass_time(workers, clock, 45.0)
        self.assertEqual(thread.edits, ["B", "A"])

    async def test_a_thread_without_a_live_session_is_not_renamed(self) -> None:
        workers = self.workers(lambda _thread_id, _epoch: None, FakeClock())
        workers.rename(1, "new", "epoch-1")
        await self.settle()
        self.assertIsNone(workers.worker(1).task)

    async def test_a_rename_asked_for_by_an_earlier_session_epoch_is_dropped(self) -> None:
        thread, clock = FakeThread("old"), FakeClock()
        owner = {"epoch": "epoch-1"}
        workers = self.workers(lambda _thread_id, epoch: thread if epoch == owner["epoch"] else None, clock)
        for name in ("a", "b", "c"):
            workers.rename(1, name, "epoch-1")
            await self.settle()
        self.assertEqual(thread.edits, ["a", "b"])
        # The session reconnects under a new epoch while "c" waits for the window.
        owner["epoch"] = "epoch-2"
        await self.pass_time(workers, clock, RENAME_WINDOW_SECONDS)
        self.assertEqual(thread.edits, ["a", "b"])

    async def test_the_last_applied_name_decides_whether_a_rename_is_needed(self) -> None:
        thread, clock = FakeThread("A", cache_updates=False), FakeClock()
        workers = self.workers(lambda _thread_id, _epoch: thread, clock)
        workers.rename(1, "B", "epoch-1")
        await self.settle()
        # The cache still says A, but Discord shows B; asking for A again must rename.
        workers.rename(1, "A", "epoch-1")
        await self.settle()
        self.assertEqual(thread.edits, ["B", "A"])

    async def test_idle_workers_are_forgotten_once_their_rename_window_has_passed(self) -> None:
        clock = FakeClock()
        threads = {1: FakeThread("one"), 2: FakeThread("two")}
        workers = self.workers(lambda thread_id, _epoch: threads[thread_id], clock)
        workers.rename(1, "one renamed", "epoch-1")
        await self.settle()
        clock.now += RENAME_WINDOW_SECONDS
        workers.rename(2, "two renamed", "epoch-1")
        await self.settle()
        self.assertEqual(set(workers.workers), {2})
