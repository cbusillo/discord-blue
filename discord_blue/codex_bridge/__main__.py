from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from discord_blue.codex_bridge.bridge import CodexBridge
from discord_blue.codex_bridge.config import DEFAULT_CONFIG_PATH, BridgeConfig, load_config, socket_for_home


async def run_bridges(config: BridgeConfig, homes: list[Path]) -> None:
    # A connection failure is retried by its own bridge, without detaching other homes.
    sockets = list(dict.fromkeys(socket_for_home(home.expanduser().resolve()) for home in homes)) if homes else [config.socket_path]
    async with asyncio.TaskGroup() as group:
        for path in sockets:
            group.create_task(CodexBridge(replace(config, socket_path=path)).run())


def main() -> None:
    parser = argparse.ArgumentParser(description="Mirror live stock Codex threads into Discord Blue agent sessions.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="TOML config file")
    parser.add_argument(
        "--codex-home",
        type=Path,
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
