"""The bot as production builds it, a real discord.py client, logged in to FakeDiscord's REST API with no gateway.

The guild, its parent channel and its members are cached as a GUILD_CREATE caches them, and nothing else is: no
thread event reaches the client, so every session thread stays out of discord.py's cache, as a reopened thread does
until its gateway update arrives. `message_create`, `reaction_add` and `app_command` feed gateway events through
discord.py's own parsers, so they reach the cog's listeners and slash commands with the objects Discord's events
would produce. `running_cog` serves the agent-session cog's WebSocket on such a client.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import unquote

from aiohttp import web
from aiohttp.test_utils import TestServer
from discord import ClientUser
from discord.http import Route
from discord.utils import time_snowflake

from discord_blue.config import AgentSessionConfig, Config, DiscordConfig
from discord_blue.doodads.agent_session_doodad import AgentSessionDoodad
from discord_blue.plugs.discord_plug import BlueBot
from tests.fake_discord import ADD_REACTION, BOT_ID, EPOCH, GUILD_ID, PARENT_ID, FakeDiscord, FakeMessage, FakeThreadState, Hold

Json = dict[str, Any]
TOKEN = "cog-token"
OPERATOR_ROLE_ID, OPERATOR_ID, BYSTANDER_ID = 50, 60, 61
_INCREMENT = itertools.count()


def snowflake() -> int:
    """A Discord ID for something created now; its increment bits keep two made in the same millisecond apart."""
    return time_snowflake(datetime.now(UTC)) + next(_INCREMENT) % 4096


def member(user_id: int, *roles: int, bot: bool = False) -> Json:
    user = {"id": str(user_id), "username": f"user-{user_id}", "discriminator": "0", "avatar": None, "bot": bot}
    return {
        "user": user,
        "roles": [str(role) for role in roles],
        "joined_at": EPOCH.isoformat(),
        "deaf": False,
        "mute": False,
        "flags": 0,
    }


OPERATOR, BYSTANDER = member(OPERATOR_ID, OPERATOR_ROLE_ID), member(BYSTANDER_ID)


def guild_create(roles: dict[int, str], members: list[Json]) -> Json:
    """A GUILD_CREATE for the guild, holding the parent channel, `roles` (ID to name) and `members`, and no threads."""
    role_payloads = [
        {"id": str(role_id), "name": name, "permissions": "0", "position": position, "color": 0, "hoist": False}
        for position, (role_id, name) in enumerate({GUILD_ID: "@everyone", **roles}.items())
    ]
    parent = {"id": str(PARENT_ID), "type": 0, "guild_id": str(GUILD_ID), "name": "agent-sessions", "position": 0}
    everyone = [member(BOT_ID, bot=True), *members]
    return {
        "id": str(GUILD_ID),
        "name": "guild",
        "owner_id": str(BOT_ID),
        "unavailable": False,
        "roles": role_payloads,
        "channels": [{**parent, "permission_overwrites": []}],
        "threads": [],
        "members": everyone,
        "member_count": len(everyone),  # Every member is here, so discord.py does not ask the gateway for more.
        "emojis": [],
        "stickers": [],
        "features": [],
    }


@asynccontextmanager
async def discord_client(fake: FakeDiscord, config: Config, roles: dict[int, str], members: list[Json]) -> AsyncIterator[BlueBot]:
    """A logged-in BlueBot whose REST requests go to `fake`; the guild is cached and no thread is."""
    async with TestServer(fake.app()) as server:
        with patch.object(Route, "BASE", f"http://127.0.0.1:{server.port}/api/v10"):
            bot = BlueBot(config)
            # What Client.login does, without its application lookup or setup_hook, which loads every doodad.
            await bot._async_setup_hook()
            bot._connection.user = ClientUser(state=bot._connection, data=await bot.http.static_login("fake-token"))
            bot._connection.parse_guild_create(cast(Any, guild_create(roles, members)))
            try:
                yield bot
            finally:
                await bot.close()


@dataclass
class RunningCog:
    bot: BlueBot
    cog: AgentSessionDoodad
    url: str  # The agent-session WebSocket endpoint, served as production serves it.


@asynccontextmanager
async def running_cog(fake: FakeDiscord) -> AsyncIterator[RunningCog]:
    """The agent-session cog on a real client, with OPERATOR holding the operator role and BYSTANDER not."""
    config = cast(Config, SimpleNamespace(agent_session=AgentSessionConfig(), discord=DiscordConfig()))
    config.agent_session.enabled = True
    config.agent_session.token = TOKEN
    config.agent_session.channel_id = PARENT_ID
    config.agent_session.operator_role_name = "Operators"
    async with discord_client(fake, config, {OPERATOR_ROLE_ID: "Operators"}, [OPERATOR, BYSTANDER]) as bot:
        cog = AgentSessionDoodad(bot)
        await bot.add_cog(cog)
        app = web.Application()
        cog.bridge.register_routes(app)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            yield RunningCog(bot, cog, f"ws://127.0.0.1:{runner.addresses[0][1]}/agent-session/connect")
        finally:
            cog.bridge.threads.stop()
            await runner.cleanup()


def sent_now(channel_id: int, content: str, author_id: int) -> FakeMessage:
    """A message posted now: the bridge drops replies older than the session's attach."""
    return FakeMessage(snowflake(), channel_id, content, author_id=author_id)


def hold_reaction(fake: FakeDiscord, emoji: str) -> Hold:
    """Hold the bot's `emoji` reactions back until released: the moment a tap lands while the bot still adds it."""
    hold = Hold(*ADD_REACTION, match=lambda ids: unquote(ids["emoji"]) == emoji)
    fake.holds.append(hold)
    return hold


def offering(thread: FakeThreadState, emoji: str) -> FakeMessage | None:
    """The message on which the bot has put `emoji` for people to tap, once it shows."""
    return next((m for m in thread.messages if (emoji, BOT_ID) in m.reactions), None)


def message_create(bot: BlueBot, message: FakeMessage, author: Json) -> None:
    """Deliver MESSAGE_CREATE for `message`, sent by the guild member `author`."""
    data = {
        **message.payload(),
        "guild_id": str(GUILD_ID),
        "author": author["user"],
        "member": {key: value for key, value in author.items() if key != "user"},
    }
    bot._connection.parse_message_create(cast(Any, data))


def reaction_add(bot: BlueBot, channel_id: int, message_id: int, emoji: str, by: Json) -> None:
    """Deliver MESSAGE_REACTION_ADD for a reaction the guild member `by` added."""
    data = {
        "user_id": by["user"]["id"],
        "channel_id": str(channel_id),
        "message_id": str(message_id),
        "guild_id": str(GUILD_ID),
        "emoji": {"id": None, "name": emoji},
        "member": by,
        "type": 0,
        "burst": False,
    }
    bot._connection.parse_message_reaction_add(cast(Any, data))


def app_command(bot: BlueBot, thread: FakeThreadState, by: Json, group: str, name: str) -> int:
    """Deliver INTERACTION_CREATE for `/group name`, run by the guild member `by` in `thread`; returns its ID.

    The thread's own payload rides along, as Discord sends it, so discord.py builds the channel without its cache.
    """
    interaction_id = snowflake()
    data = {
        "id": str(interaction_id),
        "application_id": str(BOT_ID),
        "type": 2,
        "token": f"token-{interaction_id}",
        "version": 1,
        "guild_id": str(GUILD_ID),
        "channel_id": str(thread.id),
        "channel": thread.payload(),
        "member": {**by, "permissions": "0"},
        "data": {"id": "1", "name": group, "type": 1, "options": [{"name": name, "type": 1, "options": []}]},
        "app_permissions": "0",
        "attachment_size_limit": 8 * 1024 * 1024,
        "locale": "en-US",
        "entitlements": [],
    }
    bot._connection.parse_interaction_create(cast(Any, data))
    return interaction_id
