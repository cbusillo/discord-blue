from __future__ import annotations

import json
import unittest
from typing import Any

from discord_blue.claude_channel.session import PERMISSION, PERMISSION_REQUEST, WAITING_LOCALLY
from tests.fakes_discord_blue import FakeDiscordBlue
from tests.test_claude_channel import SPIKE_REQUEST, FakeClaudeCode, decision, running_channel
from tests.test_claude_channel_hooks import hook, mirrored

Json = dict[str, Any]
SUBDIRECTORY = "/work/project/sub"


async def pre_tool_use(claude: FakeClaudeCode, tool_use_id: str, tool_name: str, tool_input: Json, agent_id: str = "") -> None:
    """PreToolUse as Claude Code 2.1.284 reported it: cwd follows an earlier `cd`, tool_input is compact JSON."""
    compact = json.dumps(tool_input, separators=(",", ":"))
    await hook(
        claude, "PreToolUse", tool_use_id=tool_use_id, tool_name=tool_name, cwd=SUBDIRECTORY, tool_input=compact, agent_id=agent_id
    )


def permission_request(request_id: str, tool_name: str, preview: dict[str, str] | str) -> Json:
    """A permission request whose preview is laid out the way Claude Code lays it out."""
    if isinstance(preview, dict):
        preview = "{ " + ", ".join(f"{json.dumps(key)}: {json.dumps(value)}" for key, value in preview.items()) + " }"
    return {"request_id": request_id, "tool_name": tool_name, "description": "Do something", "input_preview": preview}


async def relay(claude: FakeClaudeCode, discord: FakeDiscordBlue, request: Json, final_input: Json | None = None) -> Json:
    """Send a permission request, then the PermissionRequest hook with the final input, in Claude Code 2.1.284's order."""
    claude.send({"method": PERMISSION_REQUEST, "params": request})
    if final_input is None:
        try:
            final_input = json.loads(request["input_preview"])
        except ValueError:
            final_input = {}
    compact = json.dumps(final_input, separators=(",", ":"))
    await hook(claude, "PermissionRequest", tool_name=request["tool_name"], tool_input=compact)
    return await discord.next("approval_request", "status_changed")


class ClaudeChannelApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_prompt_for_a_reported_call_shows_where_it_runs_and_takes_a_discord_decision(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            tool_input = {"command": "touch relay-test.txt", "description": "Create relay-test.txt file"}
            await pre_tool_use(claude, "toolu_1", "Bash", tool_input)
            approval = await relay(claude, discord, SPIKE_REQUEST)
            deny = decision(hello, "poeyw", "denied")
            self.assertEqual((await discord.control(deny))["type"], "approval_decision_ack")
            verdict = await claude.notification(PERMISSION)
            self.assertEqual((await discord.control(deny))["type"], "approval_decision_reject")

        self.assertEqual(
            (approval["type"], approval["command"], approval["cwd"], approval["reason"]),
            ("approval_request", ["touch", "relay-test.txt"], SUBDIRECTORY, "Bash: Create relay-test.txt file"),
        )
        self.assertEqual(verdict, {"request_id": "poeyw", "behavior": "deny"})

    async def test_requests_not_tied_to_one_exactly_shown_call_stay_in_the_terminal(self) -> None:
        long_command = "echo " + "a" * 4000
        cases: dict[str, tuple[list[tuple[str, Json]], Json]] = {
            "no call reported": ([], permission_request("aaaaa", "Bash", {"command": "touch a.txt"})),
            "Discord would show a different command": (
                [("toolu_b", {"command": 'echo "$(touch /tmp/x)"'})],
                permission_request("bbbbb", "Bash", {"command": 'echo "$(touch /tmp/x)"'}),
            ),
            "preview masked by Claude Code": (
                [("toolu_c", {"command": "deploy --token sk-live-secret"})],
                permission_request("ccccc", "Bash", {"command": "deploy --token [REDACTED]"}),
            ),
            # The reviewer's case: the masked production preview equals the staging call's literal input.
            "masked preview equal to another call's input": (
                [
                    ("toolu_prod", {"command": "deploy --token sk-live-secret"}),
                    ("toolu_stg", {"command": "deploy --token [REDACTED]"}),
                ],
                permission_request("ddddd", "Bash", {"command": "deploy --token [REDACTED]"}),
            ),
            "preview shortened by Claude Code": (
                [("toolu_e", {"command": long_command})],
                permission_request("eeeee", "Bash", '{ "command": "echo aaa ⋯ 3990 code points elided ⋯ aaa" }'),
            ),
            # Claude Code's sanitizing is lossy, so another unfinished Bash call could have produced any preview.
            "another unfinished call to the same tool": (
                [("toolu_f1", {"command": "touch f.txt"}), ("toolu_f2", {"command": "touch g.txt"})],
                permission_request("fffff", "Bash", {"command": "touch f.txt"}),
            ),
        }
        for case, (calls, request) in cases.items():
            with self.subTest(case):
                async with running_channel() as (claude, discord):
                    await claude.initialize()
                    await discord.next("hello")
                    for tool_use_id, tool_input in calls:
                        await pre_tool_use(claude, tool_use_id, "Bash", tool_input)
                    event = await relay(claude, discord, request)
                self.assertEqual((event["type"], event.get("message")), ("status_changed", WAITING_LOCALLY))

    async def test_a_finished_call_retires_only_the_prompt_for_that_call(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            await pre_tool_use(claude, "toolu_main", "Read", {"file_path": "/w/a.txt"})
            await relay(claude, discord, permission_request("aaaaa", "Read", {"file_path": "/w/a.txt"}))
            # A subagent's Read of another file runs the same hooks and finishes first.
            await pre_tool_use(claude, "toolu_sub", "Read", {"file_path": "/w/b.txt"}, agent_id="agent-1")
            await hook(claude, "PostToolUse", tool_use_id="toolu_sub", agent_id="agent-1")
            still_pending = await discord.control(decision(hello, "aaaaa", "approved"))
            await hook(claude, "PostToolUse", tool_use_id="toolu_main")

            await pre_tool_use(claude, "toolu_next", "Read", {"file_path": "/w/c.txt"})
            await relay(claude, discord, permission_request("ccccc", "Read", {"file_path": "/w/c.txt"}))
            await hook(claude, "PostToolUse", tool_use_id="toolu_next")
            events = await mirrored(claude, discord)

        self.assertEqual(still_pending["type"], "approval_decision_ack")
        self.assertEqual(events, [("approval_resolved", "ccccc")])

    async def test_a_request_whose_final_input_may_differ_from_the_reported_call_stays_in_the_terminal(self) -> None:
        cases: dict[str, tuple[Json, Json]] = {
            # The reviewer's case: another PreToolUse hook normalizes the input, which the preview cannot show.
            "rewritten to what the preview shows": ({"command": "rm 'report  draft.txt'"}, {"command": "rm 'report draft.txt'"}),
            # The reverse: the preview folds the rewritten input back to the reported one.
            "rewritten to what the preview hides": ({"command": "rm 'report draft.txt'"}, {"command": "rm 'report  draft.txt'"}),
            "lookalike character the preview neutralizes": (
                {"command": "rm 'report\u2019s draft.txt'"},
                {"command": "rm 'report\u2019s draft.txt'"},
            ),
        }
        for case, (reported, final) in cases.items():
            with self.subTest(case):
                async with running_channel() as (claude, discord):
                    await claude.initialize()
                    await discord.next("hello")
                    await pre_tool_use(claude, "toolu_1", "Bash", reported)
                    preview = {"command": " ".join(final["command"].split())}  # Claude Code folds whitespace runs
                    event = await relay(claude, discord, permission_request("aaaaa", "Bash", preview), final_input=final)
                self.assertEqual((event["type"], event.get("message")), ("status_changed", WAITING_LOCALLY))

    async def test_a_call_from_an_interrupted_turn_does_not_block_the_next_turn(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            await discord.next("hello")
            # Interrupting a turn fires neither PostToolUse nor Stop, so this call never finishes.
            await pre_tool_use(claude, "toolu_interrupted", "Bash", {"command": "sleep 60"})
            await hook(claude, "UserPromptSubmit", prompt="Do it differently")
            await pre_tool_use(claude, "toolu_next", "Bash", {"command": "touch b.txt"})
            approval = await relay(claude, discord, permission_request("bbbbb", "Bash", {"command": "touch b.txt"}))

        self.assertEqual(approval["type"], "approval_request")
