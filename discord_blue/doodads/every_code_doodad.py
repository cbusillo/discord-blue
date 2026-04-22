from __future__ import annotations

import logging

import discord
from discord.ext import commands

from discord_blue.doodads.every_code.bridge import EveryCodeBridge
from discord_blue.plugs.discord_plug import BlueBot

logger = logging.getLogger(__name__)


class EveryCodeDoodad(commands.Cog):
    def __init__(self, bot: BlueBot) -> None:
        self.bot = bot
        self.bridge = EveryCodeBridge(bot)

    @commands.Cog.listener("on_ready")
    async def start_bridge(self) -> None:
        if not self.bot.config.every_code.enabled:
            return
        await self.bridge.start()

    @commands.Cog.listener("on_message")
    async def route_thread_reply(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not self.bot.config.every_code.enabled:
            return
        if not self.bridge.is_operator(message.author):
            return
        delivered = await self.bridge.send_thread_reply(message)
        if delivered:
            logger.info("Delivered Every Code thread reply from %s", message.author.id)

    async def cog_unload(self) -> None:
        await self.bridge.stop()

async def setup(bot: BlueBot) -> None:
    await bot.add_cog(EveryCodeDoodad(bot))
