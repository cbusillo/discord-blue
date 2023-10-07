from typing import TypeVar, Callable
import logging
import discord
from config import Config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
config = Config()
T = TypeVar('T', bound=discord.Guild | discord.TextChannel)


class BlueBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)

    async def on_ready(self) -> None:
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")

        destination_guild = await self.blue_guild()
        destination_channel = await self.blue_bot_channel(destination_guild)

        await destination_channel.send("@cbax new bot, who dis?")

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
