from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from discord_blue.agent_client import Rejected
from discord_blue.codex_bridge.bridge import CodexBridge
from discord_blue.codex_bridge.config import BridgeConfig
from discord_blue.codex_bridge.rpc import RpcError, TransportError
from discord_blue.doodads.agent_session.launcher import HARNESSES, LAUNCHER_PATH, BUSY_CLOSE_CODE

logger = logging.getLogger(__name__)
CLAUDE_TIMEOUT_SECONDS = 30
BUSY_RETRY_SECONDS = 60.0
CLAUDE_CHANNEL = "plugin:dui@discord-blue"
CLOSED_MESSAGES = {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    output: str


RunCommand = Callable[[list[str], Path], Awaitable[CommandResult]]


@dataclass(slots=True)
class LiveBridge:
    bridge: CodexBridge | None = None


@dataclass(slots=True)
class LaunchOutcome:
    ok: bool
    message: str


def launcher_url(server_url: str) -> str:
    return urlsplit(server_url)._replace(path=LAUNCHER_PATH).geturl()


def find_repositories(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        return {}
    return {child.name: child for child in sorted(root.iterdir()) if child.is_dir() and (child / ".git").exists()}


def claude_binary() -> str:
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


def claude_argv(binary: str) -> list[str]:
    return [binary, "--bg", "--model", "opus", "--channels", CLAUDE_CHANNEL]


def first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


async def run_command(argv: list[str], cwd: Path) -> CommandResult:
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        async with asyncio.timeout(CLAUDE_TIMEOUT_SECONDS):
            output, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    return CommandResult(process.returncode or 0, output.decode(errors="replace"))


@dataclass(slots=True)
class MacLauncher:
    config: BridgeConfig
    live: LiveBridge
    developer_root: Path = field(default_factory=lambda: Path.home() / "Developer")
    run: RunCommand = run_command
    find_claude: Callable[[], str] = claude_binary
    busy_retry_seconds: float = BUSY_RETRY_SECONDS

    def hello(self) -> dict[str, object]:
        return {
            "type": "launcher_hello",
            "host_label": self.config.host_label,
            "repositories": list(find_repositories(self.developer_root)),
            "harnesses": list(HARNESSES),
        }

    async def launch(self, repository: str, harness: str) -> LaunchOutcome:
        folder = find_repositories(self.developer_root).get(repository)
        if folder is None:
            return LaunchOutcome(False, f"No repository named {repository!r} under ~/Developer.")
        if harness == "codex":
            return await self.launch_codex(folder)
        if harness == "claude":
            return await self.launch_claude(repository, folder)
        return LaunchOutcome(False, f"Unknown harness {harness!r}.")

    async def launch_codex(self, folder: Path) -> LaunchOutcome:
        bridge = self.live.bridge
        if bridge is None or bridge.rpc is None:
            return LaunchOutcome(False, "The Codex bridge is not connected to the Codex app-server.")
        try:
            await bridge.start_session(str(folder))
        except (Rejected, RpcError, TransportError) as exc:
            return LaunchOutcome(False, str(exc))
        return LaunchOutcome(True, f"Started a Codex session in {folder.name}.")

    async def launch_claude(self, repository: str, folder: Path) -> LaunchOutcome:
        try:
            result = await self.run(claude_argv(self.find_claude()), folder)
        except TimeoutError:
            return LaunchOutcome(False, f"claude did not finish starting within {CLAUDE_TIMEOUT_SECONDS} seconds.")
        except OSError as exc:
            return LaunchOutcome(False, f"Could not run claude ({type(exc).__name__}).")
        line = first_line(result.output)
        if result.returncode != 0:
            return LaunchOutcome(False, f"claude exited with {result.returncode}: {line or 'no output'}")
        return LaunchOutcome(True, line or f"Started a Claude Code session in {repository}.")

    async def answer(self, websocket: aiohttp.ClientWebSocketResponse, message: dict[str, object]) -> None:
        repository, harness = message.get("repository"), message.get("harness")
        if not isinstance(repository, str) or not isinstance(harness, str):
            outcome = LaunchOutcome(False, "The launch request named no repository or harness.")
        else:
            outcome = await self.launch_without_ending_the_bridge(repository, harness)
        logger.info("Launch %s %s: %s", harness, repository, outcome.message)
        result = {"type": "launch_result", "launch_id": message.get("launch_id"), "ok": outcome.ok, "message": outcome.message}
        try:
            await websocket.send_json(result)
        except (aiohttp.ClientError, ConnectionError) as exc:
            logger.warning("Could not report a launch result (%s)", type(exc).__name__)

    async def launch_without_ending_the_bridge(self, repository: str, harness: str) -> LaunchOutcome:
        # noinspection PyBroadException
        try:
            return await self.launch(repository, harness)
        except Exception:
            logger.exception("Launch of %s in %s failed", harness, repository)
            return LaunchOutcome(False, "The launch failed on the Mac; see the Codex bridge log.")

    async def serve(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        await websocket.send_json(self.hello())
        async with asyncio.TaskGroup() as launches:
            async for frame in websocket:
                if frame.type in CLOSED_MESSAGES:
                    break
                if frame.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    message = json.loads(frame.data)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("type") == "launch":
                    launches.create_task(self.answer(websocket, message))

    async def run_forever(self, http: aiohttp.ClientSession) -> None:
        headers = {"Authorization": f"Bearer {self.config.token}"}
        url = launcher_url(self.config.server_url)
        while True:
            retry = self.config.reconnect_seconds
            try:
                async with http.ws_connect(url, headers=headers, heartbeat=self.config.heartbeat_seconds) as websocket:
                    await self.serve(websocket)
                if websocket.close_code == BUSY_CLOSE_CODE:
                    logger.info("Another session launcher is connected; trying again in %s seconds", self.busy_retry_seconds)
                    retry = self.busy_retry_seconds
            except aiohttp.WSServerHandshakeError as exc:
                logger.warning("Discord Blue refused the session launcher (HTTP %s)", exc.status)
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError, TypeError) as exc:
                logger.warning("Session launcher connection ended: %s", type(exc).__name__)
            await asyncio.sleep(retry)


async def run_launcher(config: BridgeConfig, live: LiveBridge) -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=20)) as http:
        await MacLauncher(config, live).run_forever(http)
