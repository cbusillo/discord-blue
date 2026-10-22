from __future__ import annotations

import contextlib
import unittest
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import discord
from discord import app_commands

from discord_blue.config import DiscordConfig
from discord_blue.plugs.discord_plug import BlueBot
from tests.discord_client import OPERATOR, app_command, discord_client
from tests.fake_discord import GUILD_ID, FakeDiscord, FakeThreadState
from tests.test_attach_scenarios import until

Json = dict[str, Any]
EPHEMERAL = discord.MessageFlags(ephemeral=True).value
HIDDEN_DETAIL = "hidden-detail"


class Probe(app_commands.Group):
    @app_commands.command()
    async def raises(self, interaction: discord.Interaction[BlueBot]) -> None:
        raise RuntimeError(HIDDEN_DETAIL)

    @app_commands.command()
    async def deferred(self, interaction: discord.Interaction[BlueBot]) -> None:
        await interaction.response.defer(ephemeral=True)
        raise RuntimeError(HIDDEN_DETAIL)

    @app_commands.command()
    @app_commands.check(lambda interaction: False)
    async def denied(self, interaction: discord.Interaction[BlueBot]) -> None:
        raise AssertionError("a failed check must not run the command")


@contextlib.asynccontextmanager
async def probed_bot(fake: FakeDiscord) -> AsyncIterator[BlueBot]:
    config: Any = SimpleNamespace(discord=DiscordConfig())
    config.discord.guild_id = GUILD_ID
    async with discord_client(fake, config, {}, [OPERATOR]) as bot:
        bot.tree.add_command(Probe(name="probe", description="Probe"))
        yield bot


async def reply_to(fake: FakeDiscord, bot: BlueBot, thread: FakeThreadState, name: str) -> Json:
    interaction_id = app_command(bot, thread, OPERATOR, "probe", name)
    token = f"token-{interaction_id}"

    def answered() -> bool:
        data = fake.interaction_responses.get(interaction_id, {}).get("data") or {}
        return "content" in data or bool(fake.followups_by_token.get(token))

    if not await until(answered, timeout=5):
        raise AssertionError(f"/probe {name} got no reply")
    followups = fake.followups_by_token.get(token)
    return followups[-1] if followups else dict(fake.interaction_responses[interaction_id]["data"])


class AppCommandErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failing_command_gets_a_private_reply_through_the_real_tree(self) -> None:
        fake = FakeDiscord(latency=0.002)
        thread = fake.add_thread("probe")
        async with probed_bot(fake) as bot:
            for name in ["raises", "deferred", "denied"]:
                with self.subTest(command=name):
                    reply = await reply_to(fake, bot, thread, name)
                    self.assertTrue(reply.get("flags", 0) & EPHEMERAL)
                    self.assertTrue(reply["content"])
                    self.assertNotIn(HIDDEN_DETAIL, reply["content"])


if __name__ == "__main__":
    unittest.main()
