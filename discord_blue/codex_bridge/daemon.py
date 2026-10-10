from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from discord_blue.codex_bridge.config import TOKEN_ENV

logger = logging.getLogger(__name__)
DAEMON_START_TIMEOUT_SECONDS = 60


def daemon_environment(home: Path) -> dict[str, str]:
    # The bridge is a client of each account, not a source of its credentials or provider routing.
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in (TOKEN_ENV, "VIRTUAL_ENV") and not key.startswith(("CODEX_", "OPENAI_", "AZURE_OPENAI_"))
    }
    if virtual_env := os.environ.get("VIRTUAL_ENV"):
        directories = {str(Path(virtual_env) / directory) for directory in ("bin", "Scripts")}
        env["PATH"] = os.pathsep.join(entry for entry in env.get("PATH", "").split(os.pathsep) if entry not in directories)
    env["CODEX_HOME"] = str(home)
    return env


async def start_daemon(home: Path, codex_bin: str) -> None:
    """Use native idempotent start; never bootstrap, stop, or restart a watched daemon."""
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            codex_bin,
            "app-server",
            "daemon",
            "start",
            env=daemon_environment(home),
            # Managed children can retain their launch cwd; never pin a task worktree or removable build volume.
            cwd=Path.home(),
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
