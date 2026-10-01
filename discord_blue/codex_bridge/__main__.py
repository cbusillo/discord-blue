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
from discord_blue.codex_bridge.config import DEFAULT_CONFIG_PATH, BridgeConfig, load_config, socket_for_home

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


async def run_bridges(config: BridgeConfig, homes: list[Path]) -> None:
    # A connection failure is retried by its own bridge, without detaching other homes.
    sockets = list(dict.fromkeys(socket_for_home(home.expanduser().resolve()) for home in homes)) if homes else [config.socket_path]
    async with asyncio.TaskGroup() as group:
        for path in sockets:
            # The server's durable thread lookup includes host_label: keep it stable across home ordering changes.
            group.create_task(run_bridge(replace(config, socket_path=path)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Mirror live stock Codex threads into Discord Blue agent sessions.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="TOML config file")
    parser.add_argument(
        "--codex-home",
        type=codex_home,
        action="append",
        default=[],
        help="Mirror this CODEX_HOME's daemon (repeat for multiple accounts); overrides socket_path",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"codex bridge config error: {exc}", file=sys.stderr)
        sys.exit(2)
    with suppress(KeyboardInterrupt):
        asyncio.run(run_bridges(config, args.codex_home))


if __name__ == "__main__":
    main()
