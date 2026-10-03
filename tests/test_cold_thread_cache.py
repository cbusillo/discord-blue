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
    message_create,
    offering,
    reaction_add,
    running_cog,
    sent_now,
)
from tests.fake_discord import ADD_REACTION, BOT_ID, FakeDiscord
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
        fake.route_latency[ADD_REACTION] = 0.1  # The bot's reactions show one by one, as Discord's rate limit spaces them.
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
            paused = await next_command(websocket, "pause_current_turn")
            await websocket.close()

        self.assertEqual(paused["issued_by"], str(OPERATOR_ID))


if __name__ == "__main__":
    unittest.main()
