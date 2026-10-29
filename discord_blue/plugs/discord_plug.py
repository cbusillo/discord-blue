import logging
from typing import Callable, NoReturn, TypeVar

import discord
from discord.ext import commands

from discord_blue.config import Config
from discord_blue.health import DiscordGatewayHealth
from discord_blue.gateway_handover import GatewayHandover

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=discord.Guild | discord.TextChannel)
# discord.py's floor. A longer rate limit raises discord.RateLimited instead of sleeping inside the request, so callers
# (the agent-session thread workers) reschedule rather than hold their place for minutes.
MAX_RATELIMIT_SLEEP_SECONDS = 30.0


class HomeCommandTree(discord.app_commands.CommandTree["BlueBot"]):
    async def interaction_check(self, interaction: discord.Interaction["BlueBot"]) -> bool:
        return interaction.guild_id == self.client.config.discord.guild_id and bool(interaction.guild_id)

    async def on_error(self, interaction: discord.Interaction["BlueBot"], error: discord.app_commands.AppCommandError) -> None:
        await super().on_error(interaction, error)
        if isinstance(error, discord.app_commands.CheckFailure):
            message = "You do not have permission to use this command."
        else:
            message = "That command failed. Details are in the bot's log."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class BlueBot(commands.Bot):
    destination_guild: discord.Guild
    bot_channel: discord.TextChannel

    def __init__(self, config: Config) -> None:
        self.config = config
        self.gateway_health = DiscordGatewayHealth()
        intents = discord.Intents.all()

        super().__init__(
            command_prefix="!",
            intents=intents,
            tree_cls=HomeCommandTree,
            enable_debug_events=True,
            max_ratelimit_timeout=MAX_RATELIMIT_SLEEP_SECONDS,
        )
        self.handover = GatewayHandover(self, super().close)

    async def start(self, token: str, *, reconnect: bool = True) -> None:
        await self.handover.start(token, reconnect=reconnect)

    async def close(self) -> None:
        await self.handover.close()

    async def on_resumed(self) -> None:
        await self.handover.resumed()

    async def on_error(self, event_method: str, /, *args: object, **kwargs: object) -> None:
        import asyncio

        current = asyncio.current_task()
        if current in self.handover.replay or current in self.handover.startup:
            self.handover.status = "failed"
        await super().on_error(event_method, *args, **kwargs)

    def dispatch(self, event: str, /, *args: object, **kwargs: object) -> None:
        # Observe synchronously: queued callbacks must not revive proof after disconnect.
        if event == "disconnect":
            self.gateway_health.disconnect()
        elif event == "socket_raw_receive" and args:
            raw = args[0]
            if isinstance(raw, str):
                payload = self.gateway_health.receive(raw)
                if payload is not None:
                    self.handover.observe(payload)
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
            elif event == "raw_message_edit" and args:
                edited = args[0]
                if (
                    isinstance(edited, discord.RawMessageUpdateEvent)
                    and edited.cached_message is None
                    and edited.message is not None
                ):
                    super().dispatch("conversation_message_edit", None, edited.message)
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
            try:
                await self.load_extension(f"discord_blue.doodads.{doodad}")
            except commands.ExtensionNotFound:
                logger.warning("Skipping doodad %r from discord.loaded_doodads: no such module", doodad)

    async def on_ready(self) -> None:
        if self.user:
            logger.info(f"Logged in as {self.user} (ID: {self.user.id})")

        self.destination_guild = await self.blue_guild()
        self.bot_channel = await self.blue_bot_channel(self.destination_guild)

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
        if isinstance(channel, discord.TextChannel) and channel.guild.id == guild.id:
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
