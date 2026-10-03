"""The `/code` slash commands' gates: agent sessions enabled, and the caller an operator.

Each command runs through discord.py's own command tree, from an INTERACTION_CREATE in a live session's thread, on
the real client of `tests.discord_client`. Every command in the cog's group is covered, including any added later.
A gated call must still be answered (Discord shows "interaction failed" otherwise), privately, and must neither reach
the session nor reveal it.
"""

from __future__ import annotations

import asyncio
import contextlib
import unittest
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import discord

from discord_blue.doodads.agent_session_doodad import AgentSessionDoodad
from tests.discord_client import BYSTANDER, OPERATOR, TOKEN, RunningCog, app_command, running_cog
from tests.fake_discord import FakeDiscord, FakeThreadState
from tests.test_attach_scenarios import hello_for, marker, until

Json = dict[str, Any]
SESSION_ID = "gated-session"
EPHEMERAL = discord.MessageFlags(ephemeral=True).value
COMMANDS = [command.name for command in AgentSessionDoodad.code_group.commands]


class LiveSession:
    def __init__(self, running: RunningCog, fake: FakeDiscord, thread: FakeThreadState) -> None:
        self.running, self.fake, self.thread = running, fake, thread
        self.frames: list[Json] = []  # Everything the server sent the session after its hello_ack.

    async def run(self, by: Json, name: str) -> Json:
        """Run `/code name` as `by` in the session's thread; the response's data, once Discord has it."""
        interaction_id = app_command(self.running.bot, self.thread, by, "code", name)
        if not await until(lambda: interaction_id in self.fake.interaction_responses, timeout=5):
            raise AssertionError(f"/code {name} was never answered")
        await asyncio.sleep(0.05)  # Room for a command frame that should not be sent to arrive.
        return dict(self.fake.interaction_responses[interaction_id]["data"])


@contextlib.asynccontextmanager
async def live_session() -> AsyncIterator[LiveSession]:
    fake = FakeDiscord(latency=0.002)
    hello = hello_for(SESSION_ID)
    thread = fake.add_thread("gated", marker=marker(hello), archived=True, locked=True)
    async with running_cog(fake) as running, aiohttp.ClientSession() as http:
        websocket = await http.ws_connect(running.url, headers={"Authorization": f"Bearer {TOKEN}"})
        await websocket.send_json(hello)
        await websocket.receive_json(timeout=10)
        live = LiveSession(running, fake, thread)

        async def read() -> None:
            async for frame in websocket:
                live.frames.append(frame.json())

        reader = asyncio.create_task(read())
        try:
            yield live
        finally:
            await websocket.close()
            await asyncio.gather(reader, return_exceptions=True)


class SlashCommandGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_operator_reaches_the_session_with_every_command(self) -> None:
        """The other tests' gates are only meaningful if the same calls get through without them."""
        async with live_session() as live:
            for name in COMMANDS:
                with self.subTest(command=name):
                    before = len(live.frames)
                    response = await live.run(OPERATOR, name)
                    reached = len(live.frames) > before or SESSION_ID in str(response.get("content"))
                    self.assertTrue(reached, f"/code {name} did nothing for an operator: {response}")

    async def test_a_bystander_is_answered_privately_and_reaches_nothing(self) -> None:
        async with live_session() as live:
            for name in COMMANDS:
                with self.subTest(command=name):
                    response = await live.run(BYSTANDER, name)
                    self.assertEqual(response.get("flags"), EPHEMERAL)
                    self.assertNotIn(SESSION_ID, str(response.get("content")))
            self.assertEqual(live.frames, [], "a bystander's command reached the session")

    async def test_with_agent_sessions_disabled_no_command_reaches_the_session(self) -> None:
        async with live_session() as live:
            live.running.bot.config.agent_session.enabled = False
            for name in COMMANDS:
                with self.subTest(command=name):
                    response = await live.run(OPERATOR, name)
                    self.assertEqual(response.get("flags"), EPHEMERAL)
                    self.assertNotIn(SESSION_ID, str(response.get("content")))
            self.assertEqual(live.frames, [], "a command reached the session while agent sessions were disabled")


if __name__ == "__main__":
    unittest.main()
