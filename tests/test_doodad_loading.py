from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from discord_blue.config import DiscordConfig
from discord_blue.plugs.discord_plug import BlueBot

RETIRED_DOODAD = "retired_doodad"


class DoodadLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_listed_doodad_without_a_module_warns_and_startup_loads_the_rest(self) -> None:
        config: Any = SimpleNamespace(discord=DiscordConfig())
        config.discord.loaded_doodads = [RETIRED_DOODAD, "_doodad_template"]
        bot = BlueBot(config)
        await bot._async_setup_hook()
        self.addAsyncCleanup(bot.close)

        with self.assertLogs("discord_blue.plugs.discord_plug", level="WARNING") as logs:
            await bot.setup_hook()

        self.assertIn("discord_blue.doodads._doodad_template", bot.extensions)
        self.assertEqual(1, len(logs.records))
        self.assertIn(RETIRED_DOODAD, logs.output[0])


if __name__ == "__main__":
    unittest.main()
