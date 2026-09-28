from __future__ import annotations

import dataclasses
import json
import unittest
from typing import Any

from discord_blue.claude_channel.launch import loaded_as_channel
from discord_blue.claude_channel.session import HOOK_TOOL, MODEL_CALL, PERMISSION_REQUEST
from discord_blue.doodads.agent_session.protocol import SessionHello
from tests.fakes_discord_blue import FakeDiscordBlue
from tests.test_claude_channel import IDENTITY, FakeClaudeCode, command, decision, running_channel

Json = dict[str, Any]
MIRRORED = ("user_message", "title_changed", "turn_complete", "approval_resolved", "notice")


async def hook(claude: FakeClaudeCode, event: str, *, meta: Json | None = None, **fields: str) -> str:
    """Call the hook tool with the arguments the plugin's hooks.json substitutes, as Claude Code 2.1.284 sent them."""
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


def bash(request_id: str, command_line: str) -> Json:
    preview = json.dumps({"command": command_line, "description": "Touch a file"})
    return {"request_id": request_id, "tool_name": "Bash", "description": "Touch a file", "input_preview": preview}


class ClaudeChannelHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_hooks_mirror_typed_prompts_and_answers_but_not_injected_replies(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            await discord.control(command(hello, "cmd-1", "reply", text="Say PINEAPPLE."))
            echo = '<channel source="plugin:dui:dui" command_id="cmd-1">\nSay PINEAPPLE.\n</channel>'
            output = [
                await hook(claude, "UserPromptSubmit", session_title="dui-live-test", prompt=echo),
                await hook(claude, "Stop", last_assistant_message="PINEAPPLE"),
                await hook(claude, "UserPromptSubmit", session_title="dui-live-test", prompt="Now touch a file."),
                await hook(claude, "Stop", last_assistant_message="Done."),
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
                ("turn_complete", "Done."),
            ],
        )

    async def test_an_unnamed_session_is_titled_by_its_first_typed_prompt(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            await discord.next("hello")
            # Claude Code leaves a placeholder for a field the hook input does not have.
            await hook(claude, "UserPromptSubmit", session_title="${session_title}", prompt="Fix the login bug\nThen test it.")
            await hook(claude, "UserPromptSubmit", session_title="${session_title}", prompt="Also the logout bug")
            await hook(claude, "SessionStart", session_title="Renamed")
            events = await mirrored(claude, discord)

        self.assertEqual(
            [event for event in events if event[0] == "title_changed"],
            [("title_changed", "Fix the login bug"), ("title_changed", "Renamed")],
        )

    async def test_relayed_prompts_are_retired_once_the_terminal_answers_them(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            for request_id, command_line in (("aaaaa", "touch a.txt"), ("bbbbb", "touch   b.txt")):
                claude.send({"method": PERMISSION_REQUEST, "params": bash(request_id, command_line)})
                await discord.next("approval_request")
            # Another Bash call finishing does not answer a prompt; the one it asked about does.
            await hook(claude, "PostToolUse", tool_name="Bash", command="ls")
            await hook(claude, "PostToolUse", tool_name="Bash", command="touch b.txt")
            resolved = await discord.next("approval_resolved")
            late = await discord.control(decision(hello, "bbbbb", "approved"))
            events = await mirrored(claude, discord)

        self.assertEqual(late["type"], "approval_decision_reject")
        self.assertEqual(resolved["approval_id"], "bbbbb")
        # The turn ending retires the rest.
        self.assertEqual(events, [("approval_resolved", "aaaaa")])

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

    async def test_a_cleared_conversation_is_announced_and_retitled(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            await discord.next("hello")
            await hook(claude, "UserPromptSubmit", prompt="First task")
            await hook(claude, "SessionStart", session_id="new-conversation")
            await hook(claude, "UserPromptSubmit", session_id="new-conversation", prompt="Second task")
            events = await mirrored(claude, discord, session_id="new-conversation")

        self.assertEqual(
            events,
            [
                ("title_changed", "First task"),
                ("user_message", "First task"),
                ("notice", "This Claude Code session switched to conversation `new-conversation`."),
                ("title_changed", "Second task"),
                ("user_message", "Second task"),
            ],
        )


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
