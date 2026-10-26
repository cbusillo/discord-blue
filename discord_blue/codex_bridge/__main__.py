from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import traceback
from asyncio import sleep
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from time import monotonic

from discord_blue.codex_bridge.bridge import CodexBridge
from discord_blue.codex_bridge.accounts import supervisor_homes
from discord_blue.bridge_version import bridge_version
from discord_blue.codex_bridge.config import DEFAULT_CONFIG_PATH, BridgeConfig, load_config, socket_for_home
from discord_blue.codex_bridge.daemon import start_daemon

logger = logging.getLogger(__name__)
FAILURE_RESET_SECONDS = 60


def codex_home(value: str) -> Path:
    if not value.strip():
        raise argparse.ArgumentTypeError("CODEX_HOME must not be empty; omit --codex-home to use the configured/default socket")
    return Path(value)


async def run_bridge(config: BridgeConfig) -> None:
    delay = config.reconnect_seconds
    while True:
        started = monotonic()
        try:
            await CodexBridge(config).run()
            return
        except Exception as exc:
            if monotonic() - started >= FAILURE_RESET_SECONDS:
                delay = config.reconnect_seconds
            # Keep a malformed response or client bug in one home from ending every other mirror.
            # Exception messages can contain provider data; log only the exception type.
            frames = "".join(
                f"\n  {frame.filename}:{frame.lineno} in {frame.name}" for frame in traceback.extract_tb(exc.__traceback__)
            )
            logger.error("Codex bridge failed (%s); retrying this connection in %s seconds%s", type(exc).__name__, delay, frames)
            await sleep(delay)
            delay = min(delay * 2, max(config.reconnect_seconds, FAILURE_RESET_SECONDS))


async def run_home(config: BridgeConfig, home: Path | None, codex_bin: str) -> None:
    if home is not None:
        await start_daemon(home, codex_bin)
    await run_bridge(config)


async def run_bridges(
    config: BridgeConfig, homes: list[Path], codex_bin: str = "codex", *, include_configured_socket: bool = False
) -> None:
    # A connection failure is retried by its own bridge, without detaching other homes.
    sockets = [socket_for_home(home.expanduser().resolve()) for home in homes]
    if not sockets or include_configured_socket:
        sockets.append(config.socket_path.expanduser().resolve() if include_configured_socket else config.socket_path)
    sockets = list(dict.fromkeys(sockets))
    async with asyncio.TaskGroup() as group:
        for path in sockets:
            # Explicit custom sockets remain supported, but cannot identify a native daemon home.
            home = path.parent.parent if path == socket_for_home(path.parent.parent) else None
            # The server's durable thread lookup includes host_label: keep it stable across home ordering changes.
            group.create_task(run_home(replace(config, socket_path=path), home, codex_bin))


def main() -> None:
    parser = argparse.ArgumentParser(description="Mirror live stock Codex threads into Discord Blue agent sessions.")
    parser.add_argument("--version", action="version", version=bridge_version())
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="TOML config file")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--codex-home",
        type=codex_home,
        action="append",
        default=[],
        help="Mirror this CODEX_HOME's daemon (repeat for multiple accounts); overrides socket_path",
    )
    selection.add_argument(
        "--supervisor-config", type=Path, help="Read all OpenAI CODEX_HOME mappings from the Supervisor account TOML"
    )
    parser.add_argument("--codex-bin", default="codex", help="Codex executable for native daemon start (absolute path for launchd)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("Bridge source %s", bridge_version())
    try:
        config = load_config(args.config)
        homes = supervisor_homes(args.supervisor_config) if args.supervisor_config else args.codex_home
    except (OSError, ValueError) as exc:
        print(f"codex bridge config error: {exc}", file=sys.stderr)
        sys.exit(2)
    with suppress(KeyboardInterrupt):
        asyncio.run(run_bridges(config, homes, args.codex_bin, include_configured_socket=args.supervisor_config is not None))


if __name__ == "__main__":
    main()
