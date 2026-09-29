from __future__ import annotations

import dataclasses
import unittest
from typing import Any

from discord_blue.claude_channel.launch import loaded_as_channel
from discord_blue.claude_channel.session import CHANNEL, HOOK_TOOL, MODEL_CALL, PERMISSION_REQUEST
from discord_blue.doodads.agent_session.protocol import SessionHello
from tests.fakes_discord_blue import FakeDiscordBlue
from tests.test_claude_channel import IDENTITY, FakeClaudeCode, command, running_channel

Json = dict[str, Any]
SWITCHED = (
    "The Claude Code conversation in this thread ended (/clear or /resume); earlier replies from Discord are no longer accepted."
)
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
            await hook(claude, "UserPromptSubmit", prompt="First task")
            await hook(claude, "SessionEnd")
            # The session reconnects under a new epoch; a reply sent for the old conversation arrives late.
            after = await discord.next("hello")
            switched = await discord.next("notice")
            stale = await discord.control(command(before, "cmd-old", "reply", text="meant for the first task"))
            current = await discord.control(command(after, "cmd-new", "reply", text="for whatever runs now"))
            injected = await claude.notification(CHANNEL)
            await hook(claude, "SessionStart", session_id="new-conversation")
            await hook(claude, "UserPromptSubmit", session_id="new-conversation", prompt="Second task")
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
                ("title_changed", "Second task"),
            ],
        )


class PermissionNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_permission_prompt_posts_claude_codes_preview_as_a_preview_only(self) -> None:
        request = {
            "request_id": "poeyw",
            "tool_name": "Bash",
            "description": "Print a fence",
            "input_preview": '{ "command": "echo ```; deploy --token [REDACTED]" }',
        }
        async with running_channel() as (claude, discord):
            await claude.initialize()
            await discord.next("hello")
            claude.send({"method": PERMISSION_REQUEST, "params": request})
            notice = (await discord.next("notice"))["message"]
            leftover = await claude.settle()

        self.assertTrue(notice.startswith("Claude is waiting for approval in the terminal: `Bash`"))
        self.assertIn("Discord cannot", notice)
        # One fence around the preview: the one inside it cannot end the block early.
        self.assertEqual(notice.count("```"), 2)
        self.assertIn("[REDACTED]", notice)
        self.assertEqual(leftover, [])


class HeldReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_replies_wait_for_the_turn_to_end_and_are_dropped_when_the_conversation_changes(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            await hook(claude, "UserPromptSubmit", prompt="Long task")
            # Claude Code would queue a message sent now and deliver it even after /clear, so it waits here.
            await discord.sockets[-1].send_json(command(hello, "cmd-1", "reply", text="then do this"))
            await discord.control(command(hello, "barrier-1", "status_request"))  # Controls run in order.
            self.assertEqual(await claude.settle(), [])
            await hook(claude, "Stop", last_assistant_message="Done.")
            delivered = await claude.notification(CHANNEL)
            acked = await discord.next("command_ack")  # Held, so acknowledged only once delivered.

            echo = '<channel source="plugin:dui:dui" command_id="cmd-1">\nthen do this\n</channel>'
            await hook(claude, "UserPromptSubmit", prompt=echo)
            await discord.sockets[-1].send_json(command(hello, "cmd-2", "reply", text="and this, later"))
            await discord.control(command(hello, "barrier-2", "status_request"))
            await hook(claude, "SessionEnd")
            after = await discord.next("hello")
            dropped = [(await discord.next("notice"))["message"] for _ in range(2)][1]
            now_idle = await discord.control(command(after, "cmd-3", "reply", text="for the new conversation"))
            injected = await claude.notification(CHANNEL)
            leftover = await claude.settle()

        self.assertEqual((delivered["content"], acked["command_id"]), ("then do this", "cmd-1"))
        self.assertIn("1 Discord reply was waiting for the turn to end and not delivered", dropped)
        self.assertEqual((now_idle["type"], injected["content"]), ("command_ack", "for the new conversation"))
        self.assertEqual(leftover, [])

    async def test_a_turn_that_another_stop_hook_continues_holds_replies_again(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            await hook(claude, "UserPromptSubmit", prompt="Task")
            await hook(claude, "Stop", last_assistant_message="First pass.")
            # Another plugin's Stop hook blocked the stop, so Claude keeps working.
            await hook(claude, "PreToolUse")
            await discord.sockets[-1].send_json(command(hello, "cmd-1", "reply", text="after the long tool call"))
            await discord.control(command(hello, "barrier", "status_request"))
            held = await claude.settle()
            await hook(claude, "PostToolUse")
            await hook(claude, "Stop", last_assistant_message="Done.")
            delivered = await claude.notification(CHANNEL)

        self.assertEqual(held, [])
        self.assertEqual(delivered["content"], "after the long tool call")

    async def test_each_idle_moment_releases_one_held_reply(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            await hook(claude, "UserPromptSubmit", prompt="Task")
            for command_id, text in (("cmd-1", "first"), ("cmd-2", "second")):
                await discord.sockets[-1].send_json(command(hello, command_id, "reply", text=text))
            await discord.control(command(hello, "barrier", "status_request"))
            await hook(claude, "Stop", last_assistant_message="Done.")
            after_first_stop = [message["params"]["content"] for message in await claude.settle()]
            claude.received.clear()
            echo = '<channel source="plugin:dui:dui" command_id="cmd-1">\nfirst\n</channel>'
            await hook(claude, "UserPromptSubmit", prompt=echo)
            await hook(claude, "Stop", last_assistant_message="Answered first.")
            after_second_stop = [message["params"]["content"] for message in await claude.settle()]

        self.assertEqual((after_first_stop, after_second_stop), (["first"], ["second"]))

    async def test_an_idle_prompt_releases_replies_held_by_an_interrupted_turn(self) -> None:
        async with running_channel() as (claude, discord):
            await claude.initialize()
            hello = await discord.next("hello")
            await hook(claude, "UserPromptSubmit", prompt="Task the owner interrupts; Stop does not run")
            await discord.sockets[-1].send_json(command(hello, "cmd-1", "reply", text="try another way"))
            await discord.control(command(hello, "barrier", "status_request"))
            self.assertEqual(await claude.settle(), [])
            await hook(claude, "Notification")
            delivered = await claude.notification(CHANNEL)

        self.assertEqual(delivered["content"], "try another way")


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
