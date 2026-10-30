from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiohttp

from discord_blue.claude_channel.launch import loaded_as_channel, launch_prompt, matches_launch_prompt
from discord_blue.claude_channel.session import (
    ANSWERED_IN_TERMINAL,
    CHANNEL,
    CONTINUING,
    HOOK_TOOL,
    MODEL_CALL,
    ClaudeSession,
    waiting_message,
)
from discord_blue.doodads.agent_session.protocol import LAUNCH_BRIEF_ORIGIN, SERVER_FEATURES, SessionHello
from tests.fakes_discord_blue import FakeDiscordBlue
from tests.test_thread_titles import HARNESS_PROMPTS
from tests.test_claude_channel import IDENTITY, FakeClaudeCode, command, decision, running_channel

Json = dict[str, Any]
SWITCHED = (
    "The Claude Code conversation in this thread ended (/clear or /resume); earlier replies from Discord are no longer accepted."
)
MIRRORED = ("user_message", "title_changed", "turn_complete", "approval_resolved", "notice")


async def hook(claude: FakeClaudeCode, event: str, *, meta: Json | None = None, **fields: str) -> str:
    params: Json = {"name": HOOK_TOOL["name"], "arguments": {"event": event, "session_id": IDENTITY.session_id, **fields}}
    if meta is not None:
        params["_meta"] = meta
    response = await claude.request("tools/call", params)
    return str(response["result"]["content"][0]["text"])


async def mirrored(
    claude: FakeClaudeCode, discord: FakeDiscordBlue, session_id: str = IDENTITY.session_id
) -> list[tuple[str, object]]:
    """Every event mirrored so far. Events keep their order, so a last Stop hook's answer marks the end."""
    await hook(claude, "Stop", last_assistant_message="END", session_id=session_id)
    events = []
    while (event := await discord.next(*MIRRORED)).get("assistant_message") != "END":
        keys = ("assistant_message", "message", "title", "approval_id")
        events.append((event["type"], next(event[key] for key in keys if event.get(key))))
    return events


def record(**fields: object) -> str:
    return json.dumps(fields) + "\n"


def said(text: str) -> str:
    return record(type="assistant", message={"role": "assistant", "content": [{"type": "text", "text": text}]})


def stopped(stop: str, verdict: str = "") -> str:
    """One Stop invocation's records: an optional hook attachment that sends Claude back, then the hooks' summary."""
    attachment = record(type="attachment", attachment={"type": verdict, "hookEvent": "Stop", "toolUseID": stop})
    return (attachment if verdict else "") + record(type="system", subtype="stop_hook_summary", toolUseID=stop)


def append(transcript: Path, *records: str) -> None:
    with transcript.open("a") as lines:
        lines.write("".join(records))


class ClaudeChannelHookTests(unittest.IsolatedAsyncioTestCase):
    def test_launch_identity_uses_the_explicit_supervisor_payload(self) -> None:
        brief = "Brief from the Supervisor.\nDo the work."
        self.assertEqual(launch_prompt([f"/bin/claude --disallowedTools AskUserQuestion -- {brief}"]), brief)
        for command_line in (
            f"/bin/claude --resume {IDENTITY.session_id} -- {brief}",
            f"/bin/claude --continue -- {brief}",
            f"/bin/claude --no-session-persistence -- {brief}",
            "/bin/claude bg-session",
            "/bin/claude",
            "/bin/claude -- ",
        ):
            self.assertIsNone(launch_prompt([command_line]))

    def test_multiline_launch_matches_the_observed_macos_ps_rendering(self) -> None:
        prompt = "Brief first line\nSecond line “hello” with a tab\tand literal \\012."
        rendered = r"Brief first line\012Second line “hello” with a tab\011and literal \012."
        self.assertTrue(matches_launch_prompt(rendered, prompt))
        self.assertFalse(matches_launch_prompt(rendered, "Human correction"))

    async def test_launch_origin_requires_a_matching_brief_and_unused_transcript(self) -> None:
        for launch_text, transcript_record, expected in (
            ("Initial input", None, LAUNCH_BRIEF_ORIGIN),
            ("Initial input", {"type": "system"}, LAUNCH_BRIEF_ORIGIN),
            (None, {"type": "system"}, None),
            ("Different brief", {"type": "system"}, None),
            ("Initial input", {"type": "user", "message": "Earlier brief"}, None),
        ):
            with tempfile.TemporaryDirectory() as directory:
                transcript = Path(directory) / "session.jsonl"
                if transcript_record is not None:
                    transcript.write_text(json.dumps(transcript_record) + "\n")
                identity = dataclasses.replace(IDENTITY, spawned=True, launch_prompt=launch_text)
                async with running_channel(identity=identity) as (claude, discord):
                    await claude.initialize()
                    await discord.next("hello")
                    await hook(claude, "UserPromptSubmit", prompt="Initial input", transcript_path=str(transcript))
                    first = await discord.next("user_message")
                    await hook(claude, "UserPromptSubmit", prompt="Initial input", transcript_path=str(transcript))
                    second = await discord.next("user_message")
                    self.assertEqual(first.get("input_origin"), expected)
                    self.assertNotIn("input_origin", second)

    async def test_only_the_current_proven_scheduled_prompt_is_hidden(self) -> None:
        prompt = "Check the build and report any failures"
        for origin, meta, record_id, hook_id, expected in (
            ("scheduled", True, "scheduled-1", "scheduled-1", []),
            ("human", False, "human-1", "human-1", [("title_changed", prompt), ("user_message", prompt)]),
            ("scheduled", True, "earlier-1", "human-1", [("title_changed", prompt), ("user_message", prompt)]),
            ("scheduled", True, "earlier-1", "", [("title_changed", prompt), ("user_message", prompt)]),
            ("scheduled", False, "human-1", "human-1", [("title_changed", prompt), ("user_message", prompt)]),
        ):
            with self.subTest(origin=origin, meta=meta, record_id=record_id, hook_id=hook_id):
                with tempfile.TemporaryDirectory() as directory:
                    transcript = Path(directory) / "session.jsonl"
                    record = {
                        "type": "user",
                        "promptId": record_id,
                        "turnOrigin": origin,
                        "isMeta": meta,
                        "promptSource": "system" if meta else "typed",
                        "message": {"role": "user", "content": prompt},
                    }
                    transcript.write_text(json.dumps(record) + "\n")
                    async with running_channel() as (claude, discord):
                        await claude.initialize()
                        await discord.next("hello")
                        await hook(claude, "UserPromptSubmit", prompt=prompt, prompt_id=hook_id, transcript_path=str(transcript))
                        await hook(claude, "Stop", last_assistant_message="Build passed")
                        events = await mirrored(claude, discord)
                self.assertEqual(events, [*expected, ("turn_complete", "Build passed")])

    async def test_scheduled_provenance_can_arrive_after_the_submit_hook(self) -> None:
        prompt = "Check the build and report any failures"
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text("")
            async with running_channel() as (claude, discord):
                await claude.initialize()
                await discord.next("hello")
                await hook(claude, "UserPromptSubmit", prompt=prompt, prompt_id="scheduled-1", transcript_path=str(transcript))
                transcript.write_text(
                    json.dumps(
                        {
                            "type": "user",
                            "promptId": "scheduled-1",
                            "isMeta": True,
                            "promptSource": "system",
                            "turnOrigin": "scheduled",
                        }
                    )
                    + "\n"
                )
                await hook(claude, "PreToolUse")
                await hook(claude, "Stop", last_assistant_message="Build passed")
                events = await mirrored(claude, discord)
        self.assertEqual(events, [("turn_complete", "Build passed")])

    async def test_clean_eof_decides_pending_input_before_ending_the_session(self) -> None:
        prompt = "Human request interrupted before a tool"
        for scheduled in (False, True):
            with self.subTest(scheduled=scheduled), tempfile.TemporaryDirectory() as directory:
                transcript = Path(directory) / "session.jsonl"
                transcript.write_text("")
                async with running_channel() as (claude, discord):
                    await claude.initialize()
                    await discord.next("hello")
                    await hook(claude, "UserPromptSubmit", prompt=prompt, prompt_id="pending-1", transcript_path=str(transcript))
                    if scheduled:
                        transcript.write_text(
                            json.dumps(
                                {
                                    "type": "user",
                                    "promptId": "pending-1",
                                    "isMeta": True,
                                    "promptSource": "system",
                                    "turnOrigin": "scheduled",
                                }
                            )
                            + "\n"
                        )
                events = []
                while not discord.received.empty():
                    events.append(discord.received.get_nowait())
                inputs = [event for event in events if event["type"] == "user_message"]
                self.assertEqual([event["message"] for event in inputs], [] if scheduled else [prompt])
                self.assertEqual(events[-1]["type"], "session_end")
                if inputs:
                    self.assertLess(events.index(inputs[0]), len(events) - 1)
                    self.assertEqual(inputs[0]["session_epoch"], events[-1]["session_epoch"])

    async def test_clean_eof_stops_when_the_terminal_event_cannot_send(self) -> None:
        terminal_send_started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_send(session: ClaudeSession, websocket: aiohttp.ClientWebSocketResponse, message: Json) -> None:
            if message["type"] == "session_end":
                terminal_send_started.set()
                await release.wait()
            await super(ClaudeSession, session).send(websocket, message)

        async def close_channel() -> None:
            async with running_channel() as (claude, discord):
                await claude.initialize()
                await discord.next("hello")
                await hook(claude, "UserPromptSubmit", prompt="Human request", prompt_id="pending-1")
            self.assertTrue(all(socket.closed for socket in discord.sockets))

        with (
            patch.object(ClaudeSession, "send", blocked_send),
            patch("discord_blue.claude_channel.session.SESSION_END_TIMEOUT_SECONDS", 0.02),
        ):
            await asyncio.wait_for(close_channel(), timeout=1)
        self.assertTrue(terminal_send_started.is_set())

    async def test_hooks_mirror_typed_prompts_and_answers_but_not_injected_replies(self) -> None:
        answer = (
            "Open [Testing → Managed secrets](https://example.com/testing/managed-secrets).\n\n"
            "1. Enter the key privately.\n2. Reply entered.\n\nBare URL: https://example.com/testing/managed-secrets"
        )
        async with running_channel() as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            await discord.control(command(hello, "cmd-1", "reply", text="Say PINEAPPLE."))
            echo = '<channel source="plugin:dui:dui" command_id="cmd-1">\nSay PINEAPPLE.\n</channel>'
            output = [
                await hook(claude, "UserPromptSubmit", session_title="dui-live-test", prompt=echo),
                await hook(claude, "Stop", last_assistant_message="PINEAPPLE"),
                await hook(claude, "UserPromptSubmit", session_title="dui-live-test", prompt="Now touch a file."),
                await hook(claude, "Stop", last_assistant_message=answer),
            ]
            events = await mirrored(claude, discord)

        # Hook output becomes model context, so the tool always answers with nothing.
        self.assertEqual(output, ["", "", "", ""])
        self.assertEqual(
            events,
            [
                ("title_changed", "dui-live-test"),
                ("turn_complete", "PINEAPPLE"),
                ("user_message", "Now touch a file."),
                ("turn_complete", answer),
            ],
        )

    async def test_prompts_claude_code_injects_are_not_mirrored_and_slash_commands_are_shortened(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            await discord.next("hello")
            for prompt in HARNESS_PROMPTS.values():
                await hook(claude, "UserPromptSubmit", prompt=prompt)
            slash = (
                "<command-message>review is running…</command-message>\n"
                "<command-name>/review</command-name>\n<command-args>152</command-args>"
            )
            await hook(claude, "UserPromptSubmit", prompt=slash)
            await hook(claude, "UserPromptSubmit", prompt="Fix the login bug in the auth flow")
            events = await mirrored(claude, discord)

        self.assertEqual(
            events,
            [
                ("user_message", "/review 152"),
                ("title_changed", "Fix the login bug in the auth flow"),
                ("user_message", "Fix the login bug in the auth flow"),
            ],
        )

    async def test_the_label_is_the_user_name_else_claudes_ai_title_else_the_first_substantial_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text("")
            path = str(transcript)
            async with running_channel() as (claude, discord):
                await claude.initialize()
                await discord.next("hello")
                # Claude Code leaves a placeholder for a field the hook input does not have.
                for prompt in (
                    '<pasted_content id="fc27">',
                    "continue",
                    "Fix the login bug\nThen test it.",
                    "Now the logout bug please",
                ):
                    await hook(claude, "UserPromptSubmit", session_title="${session_title}", prompt=prompt, transcript_path=path)
                # Claude Code names the session itself as it answers.
                with transcript.open("a") as lines:
                    lines.write(json.dumps({"type": "ai-title", "aiTitle": "Login bug triage", "sessionId": "s"}) + "\n")
                await hook(claude, "Stop", last_assistant_message="Done.", transcript_path=path)
                # The user names it with /rename.
                with transcript.open("a") as lines:
                    lines.write(json.dumps({"type": "custom-title", "customTitle": "auth-refactor", "sessionId": "s"}) + "\n")
                await hook(claude, "UserPromptSubmit", prompt="And also update the docs", transcript_path=path)
                events = await mirrored(claude, discord)

        self.assertEqual(
            [event for event in events if event[0] == "title_changed"],
            [
                ("title_changed", "Fix the login bug Then test it."),
                ("title_changed", "Login bug triage"),
                ("title_changed", "auth-refactor"),
            ],
        )

    async def test_a_final_answer_another_stop_hook_sends_back_is_never_posted(self) -> None:
        # A fast rejecting hook can be recorded before this session's own Stop hook even runs.
        # Hook output such as a failing test report can be large.
        report = "x" * 300_000
        cases = (("hook_blocking_error", False), ("hook_blocking_error", True), ("hook_additional_context", False))
        for verdict, early in cases:
            with self.subTest(verdict=verdict, early=early), tempfile.TemporaryDirectory() as directory:
                transcript = Path(directory) / "session.jsonl"
                attachment = record(
                    type="attachment", attachment={"type": verdict, "hookEvent": "Stop", "toolUseID": "s1", "content": report}
                )
                transcript.write_text(said("**Q1.** Draft?") + (attachment if early else ""))
                path = str(transcript)
                async with running_channel() as (claude, discord):
                    await claude.initialize()
                    await discord.next("hello")
                    await hook(claude, "UserPromptSubmit", prompt="Ask me something", transcript_path=path)
                    await hook(claude, "Stop", last_assistant_message="**Q1.** Draft?", transcript_path=path)
                    summary = record(type="system", subtype="stop_hook_summary", toolUseID="s1")
                    append(transcript, summary if early else stopped("s1", verdict), said("**Q1.** Rewritten?"))
                    await hook(claude, "Stop", last_assistant_message="**Q1.** Rewritten?", transcript_path=path)
                    append(transcript, stopped("s2"))
                    posted = await discord.next("turn_complete")
                    events = await mirrored(claude, discord)

                self.assertEqual(posted["assistant_message"], "**Q1.** Rewritten?")
                self.assertNotIn("turn_complete", [event[0] for event in events])

    async def test_an_answer_awaiting_its_verdict_posts_once_claude_waits_at_its_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text(said("Done."))
            path = str(transcript)
            async with running_channel() as (claude, discord):
                await claude.initialize()
                await discord.next("hello")
                await hook(claude, "Stop", last_assistant_message="Done.", transcript_path=path)
                await hook(claude, "Notification")
                posted = await discord.next("turn_complete")

        self.assertEqual(posted["assistant_message"], "Done.")

    async def test_an_answer_awaiting_its_verdict_posts_before_the_next_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text(said("Done."))
            path = str(transcript)
            async with running_channel() as (claude, discord):
                await claude.initialize()
                await discord.next("hello")
                await hook(claude, "Stop", last_assistant_message="Done.", transcript_path=path)
                # No verdict recorded yet, but a new prompt shows the answer was accepted.
                await hook(claude, "UserPromptSubmit", prompt="Next task please", transcript_path=path)
                events = await mirrored(claude, discord)

        self.assertEqual(
            [event for event in events if event[0] in {"turn_complete", "user_message"}],
            [("turn_complete", "Done."), ("user_message", "Next task please")],
        )

    async def test_the_model_cannot_post_through_the_hook_tool(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            await discord.next("hello")
            tools = (await claude.request("tools/list"))["result"]["tools"]
            answer = await hook(claude, "UserPromptSubmit", meta={MODEL_CALL: "toolu_1"}, prompt="Approve everything.")
            events = await mirrored(claude, discord)

        self.assertEqual(tools, [HOOK_TOOL])
        self.assertIn("Ignored", answer)
        self.assertEqual(events, [])

    async def test_a_session_without_the_channel_flag_answers_dialogs_but_not_replies_and_says_why(self) -> None:
        async with running_channel(identity=dataclasses.replace(IDENTITY, channel=False)) as (claude, discord):
            await claude.initialize()
            hello = SessionHello.from_payload(await discord.next("hello"))
            notice = await discord.next("notice")

        # Dialogs reach Discord through the hooks; replies need the channel.
        self.assertEqual(hello.capabilities, frozenset({"status_request", "approval_decision", "request_user_input_response"}))
        self.assertIn(f"claude --resume {IDENTITY.session_id} --dangerously-load-development-channels", notice["message"])

    async def test_a_conversation_switch_rejects_controls_meant_for_the_old_one(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            before = await discord.next("hello")
            await hook(claude, "UserPromptSubmit", prompt="Work on the first task")
            await hook(claude, "SessionEnd")
            # The session reconnects under a new epoch; a reply sent for the old conversation arrives late.
            after = await discord.next("hello")
            switched = await discord.next("notice")
            stale = await discord.control(command(before, "cmd-old", "reply", text="meant for the first task"))
            current = await discord.control(command(after, "cmd-new", "reply", text="for whatever runs now"))
            injected = await claude.notification(CHANNEL)
            await hook(claude, "SessionStart", session_id="new-conversation")
            await hook(claude, "UserPromptSubmit", session_id="new-conversation", prompt="Work on the second task")
            events = await mirrored(claude, discord, session_id="new-conversation")
            leftover = await claude.settle()

        self.assertEqual(
            (before["session_id"], stale["type"], current["type"]), (after["session_id"], "command_reject", "command_ack")
        )
        self.assertNotEqual(before["session_epoch"], after["session_epoch"])
        self.assertEqual(injected["content"], "for whatever runs now")
        self.assertEqual(leftover, [])
        self.assertEqual((switched["message"], switched["session_epoch"]), (SWITCHED, after["session_epoch"]))
        self.assertEqual(
            [event for event in events if event[0] in ("title_changed", "notice")],
            [
                ("notice", "This Claude Code session is now on conversation `new-conversation`."),
                ("title_changed", "Work on the second task"),
            ],
        )


FEATURES = sorted(SERVER_FEATURES)
COMMAND = {"command": "touch notes.txt", "description": "Create notes.txt"}
QUESTION = {
    "questions": [
        {
            "question": "Which color?",
            "header": "Color",
            "options": [{"label": "Red", "description": "Warm"}, {"label": "Blue", "description": "Cool"}],
            "multiSelect": False,
        },
        {
            "question": "Which sizes?",
            "header": "Sizes",
            "options": [{"label": "S", "description": ""}, {"label": "M", "description": ""}, {"label": "L", "description": ""}],
            "multiSelect": True,
        },
    ]
}


def open_dialog(claude: FakeClaudeCode, tool_name: str, tool_input: Json, **fields: str) -> int:
    """Send the PermissionRequest hook call Claude Code makes while a dialog is open; its answer comes later."""
    claude.next_id += 1
    arguments = {
        "event": "PermissionRequest",
        "session_id": IDENTITY.session_id,
        "tool_name": tool_name,
        "tool_input": json.dumps(tool_input),
        "cwd": "/work/project/sub",
        "prompt_id": "prompt-1",
        **fields,
    }
    claude.send({"id": claude.next_id, "method": "tools/call", "params": {"name": HOOK_TOOL["name"], "arguments": arguments}})
    return claude.next_id


async def tool_call(claude: FakeClaudeCode, tool_name: str, tool_input: Json, tool_use_id: str) -> None:
    await hook(claude, "PreToolUse", tool_name=tool_name, tool_input=json.dumps(tool_input), tool_use_id=tool_use_id)


async def hook_answer(claude: FakeClaudeCode, request_id: int) -> Json | None:
    text = (await claude.take(lambda message: message.get("id") == request_id))["result"]["content"][0]["text"]
    return json.loads(text)["hookSpecificOutput"]["decision"] if text else None


class DialogTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_command_approved_in_discord_runs_exactly_as_shown(self) -> None:
        async with running_channel(features=FEATURES) as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            await tool_call(claude, "Bash", COMMAND, "toolu_1")
            request = open_dialog(claude, "Bash", COMMAND)
            card = await discord.next("approval_request")
            acked = await discord.control(decision(hello, card["approval_id"], "approved"))
            answer = await hook_answer(claude, request)
            await claude.settle()
            # Any status before the call finishes would retire the answered card on the server.
            early = [discord.received.get_nowait()["type"] for _ in range(discord.received.qsize())]
            await hook(claude, "PostToolUse", tool_use_id="toolu_1")
            status = await discord.next("status_changed")

        self.assertEqual(
            (card["command_text"], card["cwd"], card["reason"], card["call_id"]),
            ("touch notes.txt", "/work/project/sub", "Create notes.txt", "toolu_1"),
        )
        self.assertEqual(acked["type"], "approval_decision_ack")
        self.assertEqual(answer, {"behavior": "allow", "updatedInput": COMMAND})
        self.assertNotIn("status_changed", early)
        self.assertEqual(status["message"], CONTINUING)

    async def test_a_denial_in_discord_denies_the_call(self) -> None:
        async with running_channel(features=FEATURES) as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            request = open_dialog(claude, "WebFetch", {"url": "https://example.com", "prompt": "Summarize"})
            card = await discord.next("approval_request")
            await discord.control(decision(hello, card["approval_id"], "denied"))
            answer = await hook_answer(claude, request)

        # Any other tool shows its whole input.
        self.assertEqual(card["approval_kind"], "tool")
        self.assertEqual(json.loads(card["content_text"])["input"]["url"], "https://example.com")
        self.assertEqual(answer and answer["behavior"], "deny")

    async def test_questions_are_answered_from_discord_by_question_text(self) -> None:
        async with running_channel(features=FEATURES) as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            request = open_dialog(claude, "AskUserQuestion", QUESTION)
            card = await discord.next("request_user_input")
            answers = {"q0": {"answers": ["Blue"]}, "q1": {"answers": ["S", "L"]}}
            response = {"call_id": card["call_id"], "turn_id": card["turn_id"], "response": {"answers": answers}}
            acked = await discord.control(command(hello, "cmd-1", "request_user_input_response", **response))
            answer = await hook_answer(claude, request)

        self.assertEqual([(q["isOther"], q["multiSelect"]) for q in card["questions"]], [(True, False), (False, True)])
        self.assertEqual(acked["type"], "command_ack")
        expected = {**QUESTION, "answers": {"Which color?": "Blue", "Which sizes?": ["S", "L"]}}
        self.assertEqual(answer, {"behavior": "allow", "updatedInput": expected})

    async def test_a_question_dismissed_in_discord_is_declined(self) -> None:
        async with running_channel(features=FEATURES) as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            request = open_dialog(claude, "AskUserQuestion", QUESTION)
            card = await discord.next("request_user_input")
            response = {"call_id": card["call_id"], "turn_id": card["turn_id"], "response": {"answers": {}}}
            await discord.control(command(hello, "cmd-1", "request_user_input_response", **response))
            answer = await hook_answer(claude, request)

        self.assertEqual(answer and answer["behavior"], "deny")

    async def test_a_terminal_answer_closes_the_card(self) -> None:
        async with running_channel(features=FEATURES) as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            await tool_call(claude, "Bash", COMMAND, "toolu_1")
            request = open_dialog(claude, "Bash", COMMAND)
            card = await discord.next("approval_request")
            await hook(claude, "PostToolUse", tool_use_id="toolu_1")
            resolved = await discord.next("approval_resolved")
            status = await discord.next("status_changed")
            answer = await hook_answer(claude, request)
            late = await discord.control(decision(hello, card["approval_id"], "approved"))

        self.assertEqual(resolved["approval_id"], card["approval_id"])
        self.assertEqual(status["message"], CONTINUING)
        self.assertIsNone(answer)
        self.assertEqual(late["type"], "approval_decision_reject")

    async def test_a_rejection_in_the_terminal_refuses_a_later_discord_answer(self) -> None:
        # Rejecting in the terminal interrupts the turn, and no hook reports it; the transcript records the result.
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            append(transcript, said("Creating the file."))
            async with running_channel(features=FEATURES) as (claude, discord):
                await claude.initialize()
                hello = await discord.next("hello")
                await tool_call(claude, "Bash", COMMAND, "toolu_1")
                request = open_dialog(claude, "Bash", COMMAND, transcript_path=str(transcript))
                card = await discord.next("approval_request")
                result = {"type": "tool_result", "tool_use_id": "toolu_1", "content": "The user doesn't want to proceed"}
                append(transcript, record(type="user", message={"role": "user", "content": [result]}))
                refused = await discord.control(decision(hello, card["approval_id"], "approved"))
                resolved = await discord.next("approval_resolved")
                answer = await hook_answer(claude, request)

        self.assertEqual((refused["type"], refused["reason"]), ("approval_decision_reject", ANSWERED_IN_TERMINAL))
        self.assertEqual(resolved["approval_id"], card["approval_id"])
        self.assertIsNone(answer)

    async def test_what_discord_cannot_show_whole_stays_in_the_terminal(self) -> None:
        for features, tool_input in (
            (FEATURES, {"command": "cat <<'EOF'\n```\nEOF"}),
            (FEATURES, {"file_path": "/work/big.txt", "content": "x" * 5000}),
            (FEATURES, {"command": "rm -rf build", "dangerouslyDisableSandbox": True, "extra": "x" * 5000}),
            # A server that predates tool cards.
            (["command_text", "approval_content"], {"url": "https://example.com"}),
        ):
            with self.subTest(tool_input=str(tool_input)[:40]):
                async with running_channel(features=features) as (claude, discord):
                    await claude.initialize()
                    await discord.next("hello")
                    tool_name = "Bash" if "command" in tool_input else "Write" if "content" in tool_input else "WebFetch"
                    request = open_dialog(claude, tool_name, tool_input)
                    status = await discord.next("status_changed")
                    notice = await discord.next("notice")
                    await hook(claude, "Stop", last_assistant_message="Done")
                    answer = await hook_answer(claude, request)

                self.assertEqual((status["message"], notice["message"]), (waiting_message(tool_name),) * 2)
                self.assertIsNone(answer)

    async def test_identical_parallel_calls_keep_their_own_cards(self) -> None:
        async with running_channel(features=FEATURES) as (claude, discord):
            await claude.initialize()
            await discord.next("hello")
            await tool_call(claude, "Bash", COMMAND, "toolu_a")
            await tool_call(claude, "Bash", COMMAND, "toolu_b")
            first, second = open_dialog(claude, "Bash", COMMAND), open_dialog(claude, "Bash", COMMAND)
            cards = [await discord.next("approval_request"), await discord.next("approval_request")]
            await hook(claude, "PostToolUse", tool_use_id="toolu_a")
            resolved = await discord.next("approval_resolved")
            first_answer = await hook_answer(claude, first)
            await hook(claude, "PostToolUse", tool_use_id="toolu_b")
            later = await discord.next("approval_resolved")
            second_answer = await hook_answer(claude, second)

        by_call = {card["call_id"]: card["approval_id"] for card in cards}
        self.assertEqual((resolved["approval_id"], later["approval_id"]), (by_call["toolu_a"], by_call["toolu_b"]))
        self.assertEqual((first_answer, second_answer), (None, None))

    async def test_a_finished_call_never_lends_its_id_to_a_later_dialog(self) -> None:
        for finish in ("PostToolUse", "Stop"):
            with self.subTest(finish=finish):
                async with running_channel(features=FEATURES) as (claude, discord):
                    await claude.initialize()
                    await discord.next("hello")
                    # Allowed without a dialog, then the same call needs one.
                    await tool_call(claude, "Bash", COMMAND, "toolu_old")
                    await hook(claude, finish, tool_use_id="toolu_old", last_assistant_message="Done")
                    await tool_call(claude, "Bash", COMMAND, "toolu_new")
                    open_dialog(claude, "Bash", COMMAND)
                    card = await discord.next("approval_request")

                self.assertEqual(card["call_id"], "toolu_new")

    async def test_a_terminal_only_dialog_leaves_an_open_question_answerable(self) -> None:
        # Discord Blue retires open question cards on any status change.
        async with running_channel(features=FEATURES) as (claude, discord):
            await claude.initialize()
            await discord.next("hello")
            open_dialog(claude, "AskUserQuestion", QUESTION)
            await discord.next("request_user_input")
            open_dialog(claude, "Write", {"file_path": "/work/big.txt", "content": "x" * 5000})
            following = await discord.next("status_changed", "notice")

        self.assertEqual((following["type"], following["message"]), ("notice", waiting_message("Write")))

    async def test_an_older_server_gets_no_multi_select_question(self) -> None:
        older = [feature for feature in FEATURES if feature != "multi_select"]
        single = {"questions": QUESTION["questions"][:1]}
        async with running_channel(features=older) as (claude, discord):
            await claude.initialize()
            await discord.next("hello")
            open_dialog(claude, "AskUserQuestion", QUESTION)
            status = await discord.next("status_changed", "request_user_input")
            open_dialog(claude, "AskUserQuestion", single)
            card = await discord.next("request_user_input")

        self.assertEqual(status["message"], waiting_message("AskUserQuestion"))
        self.assertEqual([q["question"] for q in card["questions"]], ["Which color?"])

    async def test_a_timed_out_hook_hands_the_dialog_back_to_the_terminal(self) -> None:
        async with running_channel(features=FEATURES) as (claude, discord):
            await claude.initialize()
            await discord.next("hello")
            request = open_dialog(claude, "Bash", COMMAND)
            card = await discord.next("approval_request")
            claude.send({"method": "notifications/cancelled", "params": {"requestId": request}})
            resolved = await discord.next("approval_resolved")
            status = await discord.next("status_changed")
            leftover = await claude.settle()

        self.assertEqual(resolved["approval_id"], card["approval_id"])
        self.assertEqual(status["message"], waiting_message("Bash"))
        # A cancelled request gets no response.
        self.assertFalse(any(message.get("id") == request for message in leftover))


class ReplyDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_reply_during_a_turn_reaches_claude_at_once(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            await hook(claude, "UserPromptSubmit", prompt="Long task")
            acked = await discord.control(command(hello, "cmd-1", "reply", text="then do this"))
            delivered = await claude.notification(CHANNEL)

        self.assertEqual((acked["type"], delivered["content"]), ("command_ack", "then do this"))


class LaunchTests(unittest.TestCase):
    def test_the_nearest_claude_command_line_decides_whether_the_channel_loaded(self) -> None:
        own = "/Users/me/.local/share/uv/tools/discord-blue/bin/python /Users/me/.local/bin/discord-blue-claude-channel"
        flagged = "claude --allow-dangerously-skip-permissions --dangerously-load-development-channels plugin:dui@discord-blue"
        cases = {
            "launched with the flag": ([own, flagged, "-zsh"], True),
            "launched without it": ([own, "claude --allow-dangerously-skip-permissions", "-zsh"], False),
            "flag for another channel only": ([own, "claude --dangerously-load-development-channels server:other"], False),
            "background session claimed from the daemon": ([own, "claude bg-spare --bg-spare /tmp/x.sock"], None),
            "no Claude Code process found": ([own, "-zsh"], None),
        }
        for case, (lines, expected) in cases.items():
            with self.subTest(case):
                self.assertIs(loaded_as_channel(lines), expected)
