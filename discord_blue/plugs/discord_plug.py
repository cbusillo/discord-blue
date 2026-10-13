import logging
import textwrap
from typing import Callable, NoReturn, TypeVar

import discord
from discord.ext import commands
from discord.ext.commands import Bot

from discord_blue.config import Config

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=discord.Guild | discord.TextChannel)
# discord.py's floor. A longer rate limit raises discord.RateLimited instead of sleeping inside the request, so callers
# (the agent-session thread workers) reschedule rather than hold their place for minutes.
MAX_RATELIMIT_SLEEP_SECONDS = 30.0


class HomeCommandTree(discord.app_commands.CommandTree["BlueBot"]):
    async def interaction_check(self, interaction: discord.Interaction["BlueBot"]) -> bool:
        return interaction.guild_id == self.client.config.discord.guild_id and bool(interaction.guild_id)


class BlueBot(commands.Bot):
    destination_guild: discord.Guild
    bot_channel: discord.TextChannel

    def __init__(self, config: Config) -> None:
        self.config = config
        intents = discord.Intents.all()

        super().__init__(
            command_prefix="!",
            intents=intents,
            tree_cls=HomeCommandTree,
            max_ratelimit_timeout=MAX_RATELIMIT_SLEEP_SECONDS,
        )

    def dispatch(self, event: str, /, *args: object, **kwargs: object) -> None:
        # Gate before Cog listeners and prefix parsing, including unrelated loaded doodads.
        guild_ids = []
        for obj in args:
            guild = getattr(obj, "guild", None) or getattr(getattr(obj, "message", None), "guild", None)
            guild_id = getattr(obj, "guild_id", None) or getattr(guild, "id", None)
            if isinstance(obj, discord.Guild):
                guild_id = obj.id
            if guild_id is not None:
                guild_ids.append(guild_id)
        foreign = any(guild_id != self.config.discord.guild_id for guild_id in guild_ids)
        if event in {"message", "message_edit", "message_delete", "interaction"} and not guild_ids:
            return
        if foreign:
            if event == "message":
                super().dispatch("conversation_message", *args, **kwargs)
            elif event == "message_edit":
                super().dispatch("conversation_message_edit", *args, **kwargs)
            elif event == "raw_message_delete":
                super().dispatch("conversation_message_delete", *args, **kwargs)
            elif event == "guild_join":
                super().dispatch("foreign_guild_join", *args, **kwargs)
            return
        super().dispatch(event, *args, **kwargs)

    async def on_foreign_guild_join(self, guild: discord.Guild) -> None:
        self.tree.clear_commands(guild=guild)
        await self.tree.sync(guild=guild)

    async def sync_home_commands(self) -> list[discord.app_commands.AppCommand]:
        guild = await self.blue_guild()
        self.tree.clear_commands(guild=guild)
        self.tree.copy_global_to(guild=guild)
        # Keep local definitions for extension reloads, but never publish global commands.
        if self.application_id is not None:
            await self.http.bulk_upsert_global_commands(self.application_id, payload=[])
        for other in self.guilds:
            if other.id != guild.id:
                self.tree.clear_commands(guild=other)
                await self.tree.sync(guild=other)
        return await self.tree.sync(guild=guild)

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
        logger.info("Closing Discord client")
        await self.close()

    async def blue_guild(self) -> discord.Guild:
        if not self.config.discord.guild_id:
            return await self.select_object(list(self.guilds), "guild", self.save_config_guild)
        guild = self.get_guild(self.config.discord.guild_id)
        if guild is not None:
            return guild
        return self.message_and_raise_error(f"Could not find guild with ID {self.config.discord.guild_id}")

    async def blue_bot_channel(self, guild: discord.Guild) -> discord.TextChannel:
        if not self.config.discord.bot_channel_id:
            return await self.select_object(list(guild.text_channels), "channel", self.save_config_bot_channel)
        channel = self.get_channel(self.config.discord.bot_channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel

        return self.message_and_raise_error(f"Could not find channel with ID {self.config.discord.bot_channel_id}")

    @staticmethod
    def message_and_raise_error(message: str, error_type: type[BaseException] = ValueError) -> NoReturn:
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

    def save_config_guild(self, guild: discord.Guild) -> None:
        self.config.discord.guild_id = guild.id
        self.config.save()

    def save_config_bot_channel(self, channel: discord.TextChannel) -> None:
        self.config.discord.bot_channel_id = channel.id
        self.config.save()

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
