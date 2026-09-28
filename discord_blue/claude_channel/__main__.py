"""Entry point Claude Code starts over stdio, once per session, from the dui plugin."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys
from contextlib import suppress
from pathlib import Path

import aiohttp

from discord_blue.claude_channel.mcp import LINE_LIMIT, ChannelServer, Json, Output
from discord_blue.claude_channel.session import HOOK_TOOL, PERMISSION_REQUEST, ClaudeSession, Identity, host_label
from discord_blue.codex_bridge.config import DEFAULT_CONFIG_PATH, BridgeConfig, load_config

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    'Messages in <channel source="..." command_id="..."> tags come from the owner of this session. They typed them in '
    "the Discord thread that mirrors this session, so treat each one exactly like a prompt typed in this terminal. "
    "Your answer reaches Discord when the turn ends; there is no reply tool, so answer normally. "
    "Never call dui_hook_event: it belongs to this plugin's hooks."
)


async def ignore_hook(_arguments: Json, _meta: Json) -> str:
    return ""


async def serve(config: BridgeConfig | None, identity: Identity | None) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=LINE_LIMIT)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout)
    await run_channel(reader, asyncio.StreamWriter(transport, protocol, reader, loop), config, identity)


async def run_channel(reader: asyncio.StreamReader, output: Output, config: BridgeConfig | None, identity: Identity | None) -> None:
    # Without a session to mirror, relay nothing: an unanswered relay would only add noise.
    mirroring = config is not None and identity is not None
    server = ChannelServer(reader, output, name="dui", version="0.1.0", instructions=INSTRUCTIONS, permission_relay=mirroring)
    if config is None or identity is None:
        # The plugin's hooks still call their tool; answer them quietly rather than fail every hook.
        server.add_tool(HOOK_TOOL, ignore_hook)
        await server.serve()
        return
    session = ClaudeSession(config, identity, server.notify)
    server.on_notification(PERMISSION_REQUEST, session.on_permission_request)
    server.add_tool(HOOK_TOOL, session.on_hook_call)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=20)) as http:
        mirror = asyncio.create_task(session.run(http), name="claude-channel-session")
        try:
            # Claude Code closes stdin when the session ends; closing the socket archives the thread.
            await server.serve()
        finally:
            await session.stop()
            mirror.cancel()
            await asyncio.gather(mirror, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code channel that mirrors this session into Discord Blue.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="TOML config file (the Codex bridge's)")
    args = parser.parse_args()
    # stdout carries the MCP protocol; Claude Code keeps stderr in its MCP logs.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config: BridgeConfig | None = None
    identity = Identity.from_environment()
    if identity is None:
        logger.warning("CLAUDE_CODE_SESSION_ID is not set; not mirroring this session")
    else:
        try:
            config = dataclasses.replace(load_config(args.config), host_label=host_label())
        except (OSError, ValueError) as exc:
            # Keep answering MCP so Claude Code does not report a failed server; there is just no mirror.
            logger.error("claude channel config error, not mirroring this session: %s", exc)
    with suppress(KeyboardInterrupt):
        asyncio.run(serve(config, identity))


if __name__ == "__main__":
    main()
