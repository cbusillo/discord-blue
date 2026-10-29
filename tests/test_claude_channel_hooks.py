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
from discord_blue.claude_channel.session import CHANNEL, HOOK_TOOL, MODEL_CALL, PERMISSION_REQUEST, ClaudeSession, waiting_message
from discord_blue.doodads.agent_session.protocol import LAUNCH_BRIEF_ORIGIN, SessionHello
from tests.fakes_discord_blue import FakeDiscordBlue
from tests.test_thread_titles import HARNESS_PROMPTS
from tests.test_claude_channel import IDENTITY, FakeClaudeCode, command, running_channel

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

    async def test_a_session_without_the_channel_flag_offers_no_controls_and_says_why(self) -> None:
        async with running_channel(identity=dataclasses.replace(IDENTITY, channel=False)) as (claude, discord):
            await claude.initialize()
            hello = SessionHello.from_payload(await discord.next("hello"))
            notice = await discord.next("notice")

        self.assertEqual(hello.capabilities, frozenset({"status_request"}))
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


class PermissionNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_permission_prompt_is_announced_without_anything_from_the_request(self) -> None:
        # Claude Code does not mask an ordinary password, so neither the preview nor the description may reach Discord.
        request = {
            "request_id": "poeyw",
            "tool_name": "Bash",
            "description": "Log in with hunter2",
            "input_preview": '{ "command": "psql postgres://app:hunter2@db/prod ````" }',
        }
        async with running_channel() as (claude, discord):
            await claude.initialize()
            await discord.next("hello")
            claude.send({"method": PERMISSION_REQUEST, "params": request})
            status = await discord.next("status_changed")
            notice = await discord.next("notice")
            leftover = await claude.settle()

        self.assertEqual((status["message"], notice["message"]), (waiting_message(request), waiting_message(request)))
        self.assertNotIn("hunter2", json.dumps([status, notice]))
        self.assertEqual(leftover, [])


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


class PermissionStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_tool_activity_does_not_claim_the_terminal_approval_is_resolved(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            await discord.next("hello")
            claude.send({"method": PERMISSION_REQUEST, "params": {"tool_name": "Bash"}})
            await discord.next("status_changed")
            await discord.next("notice")
            await hook(claude, "PostToolUse")
            resumed = await discord.next("status_changed")
            self.assertIn("unconfirmed", resumed["message"])
            await hook(claude, "PostToolUse")  # No notification flood on every tool.
            await hook(claude, "Stop", last_assistant_message="Finished")
            self.assertEqual((await discord.next())["type"], "turn_complete")


class InterruptedPermissionStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_remote_prompt_clears_an_old_terminal_approval_signal(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            claude.send({"method": PERMISSION_REQUEST, "params": {"tool_name": "Bash"}})
            await discord.next("status_changed")
            await discord.next("notice")
            await discord.control(command(hello, "cmd-next", "reply", text="new turn"))
            await claude.notification(CHANNEL)
            await hook(
                claude,
                "UserPromptSubmit",
                prompt='<channel source="plugin:dui:dui" command_id="cmd-next">new turn</channel>',
            )
            started = await discord.next("status_changed")
            self.assertIn("started", started["message"].lower())
            await hook(claude, "PostToolUse")
            await hook(claude, "Stop", last_assistant_message="Finished")
            self.assertEqual((await discord.next())["type"], "turn_complete")
