from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
import fcntl
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Any, TYPE_CHECKING, cast
from urllib.parse import urlsplit

import aiohttp
import discord
from discord.backoff import ExponentialBackoff
from discord.gateway import DiscordWebSocket
from yarl import URL

if TYPE_CHECKING:
    from discord_blue.plugs.discord_plug import BlueBot


_STATE_LOCKS: dict[Path, int] = {}
SHUTDOWN_TIMEOUT_SECONDS = 90.0
logger = logging.getLogger(__name__)


def hold_state_lock() -> None:
    path = Path.home() / ".discord-blue-owner.lock"
    if path in _STATE_LOCKS:
        return
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except BaseException:
        os.close(descriptor)
        raise
    _STATE_LOCKS[path] = descriptor


@dataclass(frozen=True)
class GatewayContinuation:
    bot_id: int
    session_id: str
    sequence: int
    gateway: str
    agents: dict[str, Any]
    conversation: dict[str, Any] | None = None

    @classmethod
    def read(cls, path: Path, bot_id: int) -> GatewayContinuation:
        value = json.loads(path.read_text())
        gateway = urlsplit(value["gateway"])
        hostname = gateway.hostname
        if (
            value["bot_id"] != bot_id
            or not isinstance(value["session_id"], str)
            or not value["session_id"]
            or type(value["sequence"]) is not int
            or value["sequence"] < 0
            or gateway.scheme != "wss"
            or not hostname
            or not hostname.endswith(".discord.gg")
            or gateway.username is not None
            or gateway.password is not None
            or gateway.port not in (None, 443)
            or not isinstance(value.get("agents", {}), dict)
            or not isinstance(value.get("conversation"), (dict, type(None)))
        ):
            raise ValueError("Invalid gateway continuation")
        return cls(
            bot_id, value["session_id"], value["sequence"], value["gateway"], value.get("agents", {}), value.get("conversation")
        )

    def save(self, path: Path) -> None:
        write_handover(path, self.__dict__)


def write_handover(path: Path, value: object) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".gateway-handover-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def use_first_connection(bot: BlueBot, socket: DiscordWebSocket) -> Iterator[None]:
    descriptor = vars(DiscordWebSocket)["from_client"]
    original = DiscordWebSocket.from_client

    async def resume(_cls: type[DiscordWebSocket], client: discord.Client, **kwargs: object) -> DiscordWebSocket:
        if client is bot:
            DiscordWebSocket.from_client = descriptor  # type: ignore[method-assign]
            return socket
        factory = cast(Callable[..., Awaitable[DiscordWebSocket]], original)
        return await factory(client, **kwargs)

    adapter = classmethod(resume)
    DiscordWebSocket.from_client = adapter  # type: ignore[method-assign, assignment]
    try:
        yield
    finally:
        if vars(DiscordWebSocket)["from_client"] is adapter:
            DiscordWebSocket.from_client = descriptor  # type: ignore[method-assign]


class GatewayHandover:
    def __init__(self, bot: BlueBot, close_client: Callable[[], Awaitable[None]]) -> None:
        self.bot = bot
        self.close_client = close_client
        self.path = Path.home() / ".discord-blue-handover.json"
        self.active = self.path.with_suffix(".active")
        self.status = "initial"
        self.connect_task: asyncio.Task[None] | None = None
        self.shutdown: asyncio.Task[None] | None = None
        self.shutdown_deadline: float | None = None
        self.events: set[asyncio.Task[Any]] = set()
        self.replay: set[asyncio.Task[Any]] = set()
        self.startup: set[asyncio.Task[Any]] = set()
        self.startup_caller: asyncio.Task[Any] | None = None
        self.buffer: list[tuple[Callable[[Any], None], Any]] = []
        self.buffering = False
        self.parsing_replay = False
        self.runtime_started = False
        self.started = False
        self.agents: dict[str, Any] = {}
        self.conversation: dict[str, Any] | None = None
        self._close_callers: set[asyncio.Task[Any]] = set()
        self._parsers: dict[str, Any] | None = None
        self._previous_factory: Any = None

    def evidence(self) -> dict[str, object]:
        caught_up = self.status == "replayed" and self.runtime_started and not self.replay
        from discord_blue.doodads.agent_session_doodad import AgentSessionDoodad

        cog = self.bot.get_cog("AgentSessionDoodad")
        return {
            "status": "caught_up" if caught_up else self.status,
            "pending_events": len(self.replay),
            "buffered_events": len(self.buffer),
            "native_fallbacks": cog.bridge.handover.fallbacks if isinstance(cog, AgentSessionDoodad) else 0,
        }

    def healthy(self) -> bool:
        return self.status == "initial" or self.evidence()["status"] == "caught_up"

    async def start(self, token: str, *, reconnect: bool) -> None:
        hold_state_lock()
        self.started = True
        await self.bot.login(token)
        user = self.bot.user
        assert user is not None
        continuation = None
        if self.path.exists():
            try:
                continuation = GatewayContinuation.read(self.path, user.id)
            except (OSError, ValueError, KeyError, TypeError):
                self.status = "failed"
        elif self.active.exists():
            self.status = "failed"
        loop = asyncio.get_running_loop()
        self._previous_factory = loop.get_task_factory()
        factory = self.task_factory
        loop.set_task_factory(factory)
        try:
            if continuation is not None:
                self.status = "resuming"
                self.agents = continuation.agents
                self.conversation = continuation.conversation
                await self.warm_cache()
                self.buffer_parsers()
                connection = self.connect_resumed(continuation, reconnect=reconnect)
            else:
                connection = self.bot.connect(reconnect=reconnect)
            connect_task = asyncio.create_task(connection)
            self.connect_task = connect_task
            await connect_task
        except asyncio.CancelledError:
            if self.shutdown is None:
                raise
        finally:
            try:
                await self.close()
            finally:
                if loop.get_task_factory() is factory:
                    loop.set_task_factory(self._previous_factory)

    async def connect_resumed(self, continuation: GatewayContinuation, *, reconnect: bool) -> None:
        backoff = ExponentialBackoff()
        while not self.bot.is_closed():
            try:
                socket = await DiscordWebSocket.from_client(
                    self.bot,
                    resume=True,
                    session=continuation.session_id,
                    sequence=continuation.sequence,
                    gateway=URL(continuation.gateway),
                )
                break
            except (OSError, aiohttp.ClientError, TimeoutError):
                if not reconnect:
                    raise
                await asyncio.sleep(backoff.delay())
        else:
            return
        self.bot.ws = socket
        with use_first_connection(self.bot, socket):
            await self.bot.connect(reconnect=reconnect)

    def task_factory(
        self, loop: asyncio.AbstractEventLoop, coroutine: Coroutine[Any, Any, Any], **kwargs: object
    ) -> asyncio.Task[Any]:
        previous_factory = self._previous_factory
        task = (
            previous_factory(loop, coroutine, **kwargs)
            if previous_factory is not None
            else cast(Callable[..., asyncio.Task[Any]], asyncio.Task)(coroutine, loop=loop, **kwargs)
        )
        parent = asyncio.current_task(loop=loop)
        if parent is not None and (parent is self.connect_task or parent is self.startup_caller):
            self.events.add(task)
            task.add_done_callback(self.events.discard)
            if self.parsing_replay or (self.buffering and parent is self.connect_task):
                self.replay.add(task)
                task.add_done_callback(self.replay.discard)
            elif parent is self.startup_caller:
                self.startup.add(task)
                task.add_done_callback(self.startup.discard)
        return task

    def buffer_parsers(self) -> None:
        parsers = dict(self.bot._connection.parsers)
        self._parsers = parsers
        self.buffering = True

        def hold(parser: Callable[[Any], None]) -> Callable[[Any], None]:
            def append(payload: dict[str, Any]) -> None:
                self.buffer.append((parser, payload))

            return append

        self.bot._connection.parsers.update(
            {name: parser if name in {"RESUMED", "READY"} else hold(parser) for name, parser in parsers.items()}
        )

    def restore_parsers(self) -> None:
        if self._parsers is not None:
            self.bot._connection.parsers.clear()
            self.bot._connection.parsers.update(self._parsers)
            self._parsers = None
        self.buffering = False

    def observe(self, event: dict[str, Any]) -> None:
        if not self.started:
            return
        if not self.runtime_started and event.get("op") == DiscordWebSocket.INVALIDATE_SESSION and event.get("d") is False:
            self.discard_checkpoint()
            self.status = "failed"
            self.restore_parsers()
            self.buffer.clear()
        if event.get("op") == 0 and event.get("t") == "READY":
            self.discard_checkpoint()
            if not self.runtime_started and self.status != "initial":
                self.status = "failed"
                self.restore_parsers()
                self.buffer.clear()
            self.record_active()

    def discard_checkpoint(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            self.status = "failed"
            logger.warning("Unable to retire Gateway continuation", exc_info=True)

    def record_active(self) -> None:
        user = self.bot.user
        try:
            write_handover(self.active, {"bot_id": user.id if user else None, "active_at": time.time()})
        except OSError:
            self.status = "failed"
            logger.warning("Unable to record active Gateway owner", exc_info=True)

    async def resumed(self) -> None:
        if not self.buffering:
            return
        self.buffering = False
        try:
            self.discard_checkpoint()
            self.record_active()
            self.startup_caller = asyncio.current_task()
            self.bot._handle_ready()
            self.bot.dispatch("ready")
            if self.startup:
                await asyncio.gather(*tuple(self.startup))
            self.parsing_replay = True
            for parser, payload in self.buffer:
                parser(payload)
            self.buffer.clear()
        except Exception:
            self.status = "failed"
            logger.warning("Gateway resume failed with %s buffered events; catch-up is unconfirmed", len(self.buffer), exc_info=True)
            self.buffer.clear()
            raise
        finally:
            self.parsing_replay = False
            self.restore_parsers()
            self.startup_caller = None
        self.runtime_started = True
        self.status = "replayed" if self.status != "failed" else "failed"
        self.bot.dispatch("handover_replayed")

    async def warm_cache(self) -> None:
        user = self.bot.user
        assert user is not None
        async for summary in self.bot.fetch_guilds(limit=None):
            guild = await self.bot.fetch_guild(summary.id)
            self.bot._connection._add_guild(guild)
            guild._add_member(await guild.fetch_member(user.id))
            for channel in await guild.fetch_channels():
                guild._add_channel(channel)
            for thread in await guild.active_threads():
                guild._add_thread(thread)
        self.bot.destination_guild = await self.bot.blue_guild()
        self.bot.bot_channel = await self.bot.blue_bot_channel(self.bot.destination_guild)
        from discord_blue.doodads.agent_session_doodad import AgentSessionDoodad

        cog = self.bot.get_cog("AgentSessionDoodad")
        if isinstance(cog, AgentSessionDoodad):
            try:
                cog.bridge.handover.load(self.agents)
            except (ValueError, KeyError, TypeError, OverflowError, OSError):
                self.status = "failed"
            try:
                cog.bridge.conversations.restore(self.conversation)
            except (ValueError, KeyError, TypeError, OverflowError, OSError):
                self.status = "failed"
            await cog.start_bridge()
            for record in cog.bridge.store.records.values():
                if record.status == "closed":
                    continue
                if self.bot.get_channel(record.thread_id) is None:
                    try:
                        restored = await self.bot.fetch_channel(record.thread_id)
                    except discord.NotFound:
                        continue
                    if isinstance(restored, discord.Thread):
                        restored.guild._add_thread(restored)

    async def close(self) -> None:
        caller = asyncio.current_task()
        if caller in self.events:
            assert caller is not None
            self._close_callers.add(caller)
            if self.startup_caller is not None:
                self._close_callers.add(self.startup_caller)
        shutdown = self.shutdown
        if shutdown is None:
            if self.shutdown_deadline is None:
                self.shutdown_deadline = asyncio.get_running_loop().time() + SHUTDOWN_TIMEOUT_SECONDS
            shutdown = asyncio.create_task(self.finish())
            self.shutdown = shutdown
        await asyncio.shield(shutdown)

    async def finish(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = self.shutdown_deadline
        if deadline is None:
            deadline = loop.time() + SHUTDOWN_TIMEOUT_SECONDS
            self.shutdown_deadline = deadline
        connect_task = self.connect_task
        if connect_task is not None and not connect_task.done():
            connect_task.cancel()
            with suppress(asyncio.CancelledError):
                await connect_task
        pending = {task for task in self.events if not task.done() and task not in self._close_callers}
        complete = not self.buffer
        if pending:
            _done, remaining = await asyncio.wait(pending, timeout=max(0, deadline - loop.time()))
            complete = complete and not remaining
        socket = self.bot.ws
        from discord_blue.doodads.agent_session_doodad import AgentSessionDoodad

        cog = self.bot.get_cog("AgentSessionDoodad")
        if isinstance(cog, AgentSessionDoodad):
            cog.bridge.handover.snapshot = None
        continuation = None
        user = self.bot.user
        if socket is not None and user is not None and socket.session_id and socket.sequence is not None:
            continuation = GatewayContinuation(user.id, socket.session_id, socket.sequence, str(socket.gateway), {})
            if socket.open:
                closing = asyncio.create_task(socket.close())
                _done, remaining = await asyncio.wait({closing}, timeout=max(0, deadline - loop.time()))
                complete = complete and not remaining
        self.restore_parsers()
        await self.close_client()
        if isinstance(cog, AgentSessionDoodad):
            snapshot = cog.bridge.handover.snapshot
            complete = complete and snapshot is not None and cog.bridge.store.error is None
            if continuation is not None and snapshot is not None:
                continuation = replace(continuation, agents=snapshot["agents"], conversation=snapshot["conversation"])
        if complete and continuation is not None and loop.time() < deadline:
            continuation.save(self.path)
