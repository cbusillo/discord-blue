"""Thread replies and reactions arriving through discord.py while the session's thread is not in its cache.

After a reattach, Discord can deliver a thread's events before the reopened thread is back in discord.py's cache
(#171). discord.py then hands the cog a message whose channel is a PartialMessageable, and a reaction whose channel
`get_channel` cannot find. These tests run the cog on a real discord.py client against FakeDiscord, so the objects
come from discord.py's own gateway parsers rather than stand-ins.

Discord also shows a bot's reactions one at a time, as its rate limit spaces them, so an operator can tap the first
of an approval's reactions while the bot is still adding the rest; that tap must count.
"""

from __future__ import annotations

import unittest
from typing import Any, cast

import aiohttp

from discord_blue.doodads.agent_session import controls as controls_module
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
    async def test_a_reply_reaches_and_shows_work_in_a_thread_that_is_not_cached(self) -> None:
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
            queued = await until(lambda: (controls_module.REACTION_QUEUED, BOT_ID) in reply.reactions, timeout=2)

            # The queued reply shows the session working, in the thread that is not cached either.
            typing = await until(lambda: ("POST", "/channels/{channel}/typing") in fake.requests, timeout=2)
            await websocket.close()

        self.assertEqual(delivered["text"], reply.content, "a reply from a bystander was delivered")
        self.assertTrue(queued, f"the reply was delivered without its queued reaction: {reply.reactions}")
        self.assertTrue(typing, "the working session showed no typing indicator")

    async def test_an_approval_answered_as_soon_as_it_shows_offers_nothing_more(self) -> None:
        """The bot adds approve, then deny. An operator who approves as soon as approve shows gets the approval sent
        and its reactions cleared; the deny the bot was still adding must not land on the answered approval."""
        fake = FakeDiscord(latency=0.002)
        deny_held = hold_reaction(fake, controls_module.REACTION_APPROVAL_DENY)
        hello = hello_for("early-approval")
        thread = fake.add_thread("early-approval", marker=marker(hello), archived=True, locked=True)
        async with running_cog(fake) as running, aiohttp.ClientSession() as http:
            websocket = await http.ws_connect(running.url, headers={"Authorization": f"Bearer {TOKEN}"})
            await websocket.send_json(hello)
            await websocket.receive_json(timeout=10)
            request = {"type": "approval_request", "session_id": "early-approval", "session_epoch": "e1", "approval_id": "a1"}
            await websocket.send_json({**request, "command": ["make", "test"], "cwd": "/w/early-approval"})
            approve = controls_module.REACTION_APPROVAL_APPROVE
            self.assertTrue(await until(lambda: offering(thread, approve) is not None, 5), "no approval was offered")
            approval = offering(thread, approve)
            assert approval is not None

            def answered() -> bool:
                text = "\n".join(
                    child.get("content", "") for container in approval.components for child in container.get("components", [])
                )
                return "Approval sent" in text and not approval.reactions

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
