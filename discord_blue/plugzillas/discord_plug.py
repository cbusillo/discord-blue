import logging
import textwrap
from typing import Callable, TypeVar

import discord
from discord.ext import commands

from discord_blue.config import config

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=discord.Guild | discord.TextChannel)


class BlueBot(commands.Bot):
    destination_guild: discord.Guild
    bot_channel: discord.TextChannel

    def __init__(self) -> None:
        self.config = config
        super().__init__(intents=discord.Intents.all(), command_prefix="!")

    async def setup_hook(self) -> None:
        await self.load_extension("discord_blue.doodads._doodad_setup")
        for doodad in self.config.discord.loaded_doodads:
            await self.load_extension(f"discord_blue.doodads.{doodad}")

    async def on_ready(self) -> None:
        if self.user:
            logger.info(f"Logged in as {self.user} (ID: {self.user.id})")

        self.destination_guild = await self.blue_guild()
        self.bot_channel = await self.blue_bot_channel(self.destination_guild)

        await self.bot_channel.send("Connected")

    async def on_signal(self, signal: int) -> None:
        logger.info(f"Received signal {signal}")
        await self.clear_commands_and_logout()

    async def clear_commands_and_logout(self) -> None:
        logger.info("Starting command clearance and logout process...")
        installed_commands = self.tree.get_commands()
        for command in installed_commands:
            self.tree.remove_command(command.name)
            logger.info(f"Successfully deleted command: {command.name}")
        sync_result = await self.tree.sync()
        logger.info(f"Sync result: {sync_result}")
        self.clear()
        await self.close()

    async def blue_guild(self) -> discord.Guild:
        if not config.discord.guild_id:
            return await self.select_object(list(self.guilds), "guild", self.save_config_guild)
        else:
            if guild := self.get_guild(config.discord.guild_id):
                return guild
            else:
                logger.error(f"Could not find guild with ID {config.discord.guild_id}")
                raise ValueError

    async def blue_bot_channel(self, guild: discord.Guild) -> discord.TextChannel:
        if not config.discord.bot_channel_id:
            return await self.select_object(list(guild.text_channels), "channel", self.save_config_bot_channel)
        else:
            channel = self.get_channel(config.discord.bot_channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel
            else:
                logger.error(f"Could not find channel with ID {config.discord.bot_channel_id}")
                raise ValueError

    @staticmethod
    async def select_object(objects: list[T], object_type: str, callback: Callable[[T], None]) -> T:
        print(f"Available {object_type}s:")
        for index, obj in enumerate(objects):
            print(f"{index + 1}: {obj.name}")

        while True:
            try:
                selected_index = int(input(f"Select a {object_type}: "))
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

    @staticmethod
    async def wrap_reply_lines(lines: str, message: discord.Message | discord.Interaction) -> None:
        """Break up messages that are longer than 2000
        chars and sends multible messages to discord"""
        if not isinstance(message.channel, discord.TextChannel):
            return
        if lines is None or lines == "":
            lines = "No lines to send"
        wrap_length = 2000 - len(message.author.mention) if hasattr(message, "author") else 2000
        lines_list = textwrap.wrap(
            lines,
            wrap_length,
            break_long_words=True,
            replace_whitespace=False,
        )
        if hasattr(message, "author") and message.author.bot is False:
            lines_list[0] = f"{message.author.mention} {lines_list[0]}"
        for line in lines_list:
            await message.channel.send(line)
