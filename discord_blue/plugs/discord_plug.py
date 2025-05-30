import logging
import textwrap
from typing import Callable, TypeVar, Type, NoReturn

import discord
from discord.ext import commands
from discord.ext.commands import Bot

from discord_blue.config import config

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=discord.Guild | discord.TextChannel)


class BlueBot(commands.Bot):
    destination_guild: discord.Guild
    bot_channel: discord.TextChannel

    def __init__(self) -> None:
        self.config = config
        intents = discord.Intents.all()

        super().__init__(
            command_prefix="!",
            intents=intents,
        )

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
        await self.bot_channel.send(f"Deleted {len(installed_commands)} commands")
        sync_result = await self.tree.sync()
        logger.info(f"Sync result: {sync_result}")
        await self.clear()
        await self.close()

    async def blue_guild(self) -> discord.Guild:
        if not config.discord.guild_id:
            return await self.select_object(list(self.guilds), "guild", self.save_config_guild)
        if guild := self.get_guild(config.discord.guild_id):
            return guild
        await self.message_and_raise_error(f"Could not find guild with ID {config.discord.guild_id}")

    async def blue_bot_channel(self, guild: discord.Guild) -> discord.TextChannel:
        if not config.discord.bot_channel_id:
            return await self.select_object(list(guild.text_channels), "channel", self.save_config_bot_channel)
        if channel := self.get_channel(config.discord.bot_channel_id):
            if isinstance(channel, discord.TextChannel):
                return channel

        await self.message_and_raise_error(f"Could not find channel with ID {config.discord.bot_channel_id}")

    @staticmethod
    async def message_and_raise_error(message: str, error_type: Type[BaseException] = ValueError) -> NoReturn:
        logging.error(message)
        raise error_type(message)

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
    async def wrap_reply_lines(lines: str, message: discord.Message | discord.Interaction[Bot]) -> None:
        if not isinstance(message.channel, discord.TextChannel):
            return
        if lines is None or lines == "":
            lines = "No lines to send"
        if isinstance(message, discord.Message):
            wrap_length = 2000 - len(message.author.mention)
        else:
            wrap_length = 2000
        lines_list = textwrap.wrap(lines, wrap_length, replace_whitespace=False)
        if isinstance(message, discord.Message) and message.author.bot is False:
            lines_list[0] = f"{message.author.mention} {lines_list[0]}"
        for line in lines_list:
            await message.channel.send(line)
