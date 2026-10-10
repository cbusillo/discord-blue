from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)
DAEMON_START_TIMEOUT_SECONDS = 60


async def start_daemon(home: Path, codex_bin: str) -> None:
    """Use native idempotent start; never bootstrap, stop, or restart a watched daemon."""
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            codex_bin,
            "app-server",
            "daemon",
            "start",
            env={**os.environ, "CODEX_HOME": str(home)},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        async with asyncio.timeout(DAEMON_START_TIMEOUT_SECONDS):
            code = await process.wait()
        if code:
            logger.error("Codex daemon start failed for %s (exit %s); socket reconnect remains active", home, code)
        else:
            logger.info("Codex native daemon start succeeded for %s", home)
    except (OSError, TimeoutError) as exc:
        # Provider/configuration output may contain secrets. Identify the home and failure without echoing it.
        logger.error("Codex daemon start failed for %s (%s); socket reconnect remains active", home, type(exc).__name__)
    finally:
        if process is not None and process.returncode is None:
            # Only reap our start command, never the independently managed daemon or its sessions.
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
