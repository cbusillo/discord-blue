"""Thread replies and reactions arriving through discord.py while the session's thread is not in its cache.

After a reattach, Discord can deliver a thread's events before the reopened thread is back in discord.py's cache
(#171). discord.py then hands the cog a message whose channel is a PartialMessageable, and a reaction whose channel
`get_channel` cannot find. These tests run the cog on a real discord.py client against FakeDiscord, so the objects
come from discord.py's own gateway parsers rather than stand-ins.

Discord also shows a bot's reactions one at a time, as its rate limit spaces them, so an operator can tap the first
of a message's controls while the bot is still adding the rest; that tap must count.
"""

from __future__ import annotations

import unittest
from typing import Any, cast

import aiohttp

from discord_blue.doodads.agent_session import bridge as bridge_module
from tests.discord_client import (
    BYSTANDER,
    BYSTANDER_ID,
    OPERATOR,
    OPERATOR_ID,
    TOKEN,
    hold_reaction,
    message_create,
    offering,
    reaction_add,
    release_after_tap,
    running_cog,
    sent_now,
)
from tests.fake_discord import BOT_ID, FakeDiscord
from tests.test_attach_scenarios import hello_for, marker, until

Json = dict[str, Any]


async def next_command(websocket: aiohttp.ClientWebSocketResponse, kind: str) -> Json:
    while True:
        frame = await websocket.receive_json(timeout=5)
        if frame.get("type") == "command" and frame.get("kind") == kind:
            return cast(Json, frame)


class ColdThreadCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_reply_and_a_reaction_reach_a_session_whose_thread_is_not_cached(self) -> None:
        fake = FakeDiscord(latency=0.002)
        hello = hello_for("cold-cache")
        thread = fake.add_thread("cold-cache", marker=marker(hello), archived=True, locked=True)
        async with running_cog(fake) as running, aiohttp.ClientSession() as http:
            websocket = await http.ws_connect(running.url, headers={"Authorization": f"Bearer {TOKEN}"})
            await websocket.send_json(hello)
            ack = await websocket.receive_json(timeout=10)
            self.assertEqual(ack.get("thread_id"), thread.id)
            self.assertIsNone(running.bot.get_channel(thread.id), "the thread is cached, so this tests nothing")

            ignored = sent_now(thread.id, "from someone who is not an operator", BYSTANDER_ID)
            thread.messages.append(ignored)
            message_create(running.bot, ignored, BYSTANDER)
            reply = sent_now(thread.id, "a reply while the thread is not cached", OPERATOR_ID)
            thread.messages.append(reply)
            message_create(running.bot, reply, OPERATOR)
            delivered = await next_command(websocket, "reply")
            queued = await until(lambda: (bridge_module.REACTION_QUEUED, BOT_ID) in reply.reactions, timeout=2)

            # The operator pauses the turn from the session controls; the reaction's thread is not cached either.
            pause = bridge_module.REACTION_CONTROL_PAUSE
            self.assertTrue(await until(lambda: offering(thread, pause) is not None, 5), "no pause control was offered")
            controls = offering(thread, pause)
            assert controls is not None
            reaction_add(running.bot, thread.id, controls.id, pause, OPERATOR)
            paused = await next_command(websocket, "pause_current_turn")
            await websocket.close()

        self.assertEqual(delivered["text"], reply.content, "a reply from a bystander was delivered")
        self.assertTrue(queued, f"the reply was delivered without its queued reaction: {reply.reactions}")
        self.assertEqual(paused["issued_by"], str(OPERATOR_ID))

    async def test_a_control_tapped_as_soon_as_it_shows_is_not_ignored(self) -> None:
        """A prompt typed in the TUI posts fresh session controls. The bot adds them one at a time, and an operator
        who taps pause as soon as it shows must not be ignored because the bot is still adding end-session."""
        fake = FakeDiscord(latency=0.002)
        end_held = hold_reaction(fake, bridge_module.REACTION_CONTROL_END)
        hello = hello_for("fresh-controls")
        thread = fake.add_thread("fresh-controls", marker=marker(hello), archived=True, locked=True)
        async with running_cog(fake) as running, aiohttp.ClientSession() as http:
            websocket = await http.ws_connect(running.url, headers={"Authorization": f"Bearer {TOKEN}"})
            await websocket.send_json(hello)
            await websocket.receive_json(timeout=10)
            typed = {"type": "user_message", "session_id": "fresh-controls", "session_epoch": "e1", "message": "typed in the TUI"}
            await websocket.send_json(typed)
            pause = bridge_module.REACTION_CONTROL_PAUSE
            self.assertTrue(await until(lambda: offering(thread, pause) is not None, 5), "no pause control was offered")
            controls = offering(thread, pause)
            assert controls is not None
            reaction_add(running.bot, thread.id, controls.id, pause, OPERATOR)
            queued = (bridge_module.REACTION_QUEUED, BOT_ID)
            try:
                paused = await next_command(websocket, "pause_current_turn")
                # Then the end-session reaction the bot was adding lands; the controls must still show only queued.
                await release_after_tap(end_held, lambda: queued in controls.reactions)
                await until(lambda: queued in controls.reactions, 5)
                final_reactions = list(controls.reactions)
            finally:
                end_held.released.set()
            await websocket.close()

        self.assertEqual(paused["issued_by"], str(OPERATOR_ID))
        self.assertEqual(final_reactions, [queued], "the bot's remaining controls landed after the tap was handled")

    async def test_an_approval_answered_as_soon_as_it_shows_offers_nothing_more(self) -> None:
        """The bot adds approve, then deny. An operator who approves as soon as approve shows gets the approval sent
        and its reactions cleared; the deny the bot was still adding must not land on the answered approval."""
        fake = FakeDiscord(latency=0.002)
        deny_held = hold_reaction(fake, bridge_module.REACTION_APPROVAL_DENY)
        hello = hello_for("early-approval")
        thread = fake.add_thread("early-approval", marker=marker(hello), archived=True, locked=True)
        async with running_cog(fake) as running, aiohttp.ClientSession() as http:
            websocket = await http.ws_connect(running.url, headers={"Authorization": f"Bearer {TOKEN}"})
            await websocket.send_json(hello)
            await websocket.receive_json(timeout=10)
            request = {"type": "approval_request", "session_id": "early-approval", "session_epoch": "e1", "approval_id": "a1"}
            await websocket.send_json({**request, "command": ["make", "test"], "cwd": "/w/early-approval"})
            approve = bridge_module.REACTION_APPROVAL_APPROVE
            self.assertTrue(await until(lambda: offering(thread, approve) is not None, 5), "no approval was offered")
            approval = offering(thread, approve)
            assert approval is not None

            def answered() -> bool:
                return "Approval sent" in approval.content and not approval.reactions

            reaction_add(running.bot, thread.id, approval.id, approve, OPERATOR)
            try:
                decision = await websocket.receive_json(timeout=5)
                await release_after_tap(deny_held, answered)
                await until(answered, 5)
                final_reactions = list(approval.reactions)
            finally:
                deny_held.released.set()
            await websocket.close()

        self.assertEqual((decision["type"], decision["decision"]), ("approval_decision", "approved"))
        self.assertEqual(final_reactions, [], "the bot's deny landed on the answered approval")


if __name__ == "__main__":
    unittest.main()
