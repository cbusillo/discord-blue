import logging
from pathlib import Path
from typing import Callable, TypeVar

import discord
from discord.ext import commands

from discord_blue.config import Config

logger = logging.getLogger(__name__)
config = Config()
T = TypeVar('T', bound=discord.Guild | discord.TextChannel)


class BlueBot(commands.Bot):
    destination_guild: discord.Guild
    bot_channel: discord.TextChannel
    config = config

    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.all(), command_prefix="!")

    async def setup_hook(self) -> None:
        doodad_path = Path(__file__).parent.parent / "doodads"
        for file in (f for f in doodad_path.glob("*.py") if f.stem != "__init__"):
            try:
                await self.load_extension(f"doodads.{file.stem}")
            except commands.ExtensionNotFound:
                logger.warning(f"Could not load extension {file.stem}")

    async def on_ready(self) -> None:
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")

        self.destination_guild = await self.blue_guild()
        self.bot_channel = await self.blue_bot_channel(self.destination_guild)

        await self.bot_channel.send("Connected")

    async def blue_guild(self) -> discord.Guild:
        if not config.discord.guild_id:
            await self.select_object(list(self.guilds), "guild", self.save_config_guild)
        else:
            return self.get_guild(config.discord.guild_id)

    async def blue_bot_channel(self, guild: discord.Guild) -> discord.TextChannel:
        if not config.discord.bot_channel_id:
            await self.select_object(list(guild.text_channels), "channel", self.save_config_bot_channel)
        else:
            return self.get_channel(config.discord.bot_channel_id)

    @staticmethod
    async def select_object(objects: list[T], object_type: str, callback: Callable[[T], None]) -> T:
        print(f"Available {object_type}s:")
        for index, obj in enumerate(objects):
            print(f"{index + 1}: {obj.name}")

        while True:
            selected_index = input(f"Select a {object_type}: ")
            try:
                selected_index = int(selected_index)
                if 0 < selected_index <= len(objects):
                    selected_object = objects[selected_index - 1]
                    callback(selected_object)
                    return selected_object
            except ValueError:
                print("Invalid input. Please use the index number.")

    @staticmethod
    def save_config_guild(guild: discord.Guild) -> None:
        config.discord.guild_id = guild.id
        config.save()

    @staticmethod
    def save_config_bot_channel(channel: discord.TextChannel) -> None:
        config.discord.bot_channel_id = channel.id
        config.save()
