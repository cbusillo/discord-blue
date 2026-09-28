from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from contextlib import suppress
from pathlib import Path

from discord_blue.codex_bridge.bridge import CodexBridge
from discord_blue.codex_bridge.config import DEFAULT_CONFIG_PATH, load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Mirror live stock Codex threads into Discord Blue agent sessions.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="TOML config file")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"codex bridge config error: {exc}", file=sys.stderr)
        sys.exit(2)
    with suppress(KeyboardInterrupt):
        asyncio.run(CodexBridge(config).run())


if __name__ == "__main__":
    main()
