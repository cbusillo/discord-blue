from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from discord_blue.agent_client import Rejected
from discord_blue.codex_bridge.accounts import LaunchAccount, launch_accounts
from discord_blue.codex_bridge.bridge import CodexBridge
from discord_blue.codex_bridge.config import BridgeConfig, socket_for_home
from discord_blue.codex_bridge.rpc import RpcError, TransportError
from discord_blue.doodads.agent_session.launcher import HARNESSES, LAUNCHER_PATH, BUSY_CLOSE_CODE, MESSAGE_LIMIT

logger = logging.getLogger(__name__)
CLAUDE_TIMEOUT_SECONDS = 30
CLAUDE_STOP_TIMEOUT_SECONDS = 10
BUSY_RETRY_SECONDS = 60.0
CLAUDE_CHANNEL = "plugin:dui@discord-blue"
CLAUDE_HOME_VARIABLE = "CLAUDE_CONFIG_DIR"
NO_MATCHING_JOB = "No job matching"
STARTED_IN_A_TERMINAL = "This Claude Code session was not started from Discord; end it in its terminal."
CLOSED_MESSAGES = {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    output: str


@dataclass(frozen=True, slots=True)
class Command:
    argv: list[str]
    cwd: Path
    claude_home: Path | None = None
    timeout_seconds: float = CLAUDE_TIMEOUT_SECONDS


RunCommand = Callable[[Command], Awaitable[CommandResult]]


@dataclass(slots=True)
class LiveBridges:
    default_socket: Path | None = None
    by_socket: dict[Path, CodexBridge] = field(default_factory=dict)

    def bridge_for(self, socket: Path | None) -> CodexBridge | None:
        socket = socket or self.default_socket
        return self.by_socket.get(socket) if socket is not None else None


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


def claude_argv(binary: str, prompt: str | None = None) -> list[str]:
    argv = [binary, "--bg", "--model", "opus", "--channels", CLAUDE_CHANNEL]
    return [*argv, "--", prompt] if prompt else argv


def claude_environment(claude_home: Path | None) -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if name != CLAUDE_HOME_VARIABLE}
    if claude_home is not None:
        environment[CLAUDE_HOME_VARIABLE] = str(claude_home)
    return environment


def short_session_id(session_id: object) -> str | None:
    if not isinstance(session_id, str):
        return None
    try:
        return str(uuid.UUID(session_id))[:8]
    except ValueError:
        return None


def first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


async def run_command(command: Command) -> CommandResult:
    process = await asyncio.create_subprocess_exec(
        *command.argv,
        cwd=command.cwd,
        env=claude_environment(command.claude_home),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async with asyncio.timeout(command.timeout_seconds):
            output, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    return CommandResult(process.returncode or 0, output.decode(errors="replace"))


@dataclass(slots=True)
class MacLauncher:
    config: BridgeConfig
    live: LiveBridges
    supervisor_config: Path | None = None
    developer_root: Path = field(default_factory=lambda: Path.home() / "Developer")
    run: RunCommand = run_command
    find_claude: Callable[[], str] = claude_binary
    busy_retry_seconds: float = BUSY_RETRY_SECONDS

    def accounts(self) -> dict[str, LaunchAccount]:
        if self.supervisor_config is None:
            return {}
        try:
            return launch_accounts(self.supervisor_config)
        except (OSError, ValueError) as exc:
            logger.warning("Session launcher cannot read named accounts (%s)", type(exc).__name__)
            return {}

    def hello(self) -> dict[str, object]:
        return {
            "type": "launcher_hello",
            "host_label": self.config.host_label,
            "repositories": list(find_repositories(self.developer_root)),
            "harnesses": list(HARNESSES),
            "accounts": [{"name": account.name, "harness": account.harness} for account in self.accounts().values()],
        }

    async def launch(
        self, repository: str, harness: str, account_name: str | None = None, prompt: str | None = None
    ) -> LaunchOutcome:
        folder = find_repositories(self.developer_root).get(repository)
        if folder is None:
            return LaunchOutcome(False, f"No repository named {repository!r} under ~/Developer.")
        if harness not in HARNESSES:
            return LaunchOutcome(False, f"Unknown harness {harness!r}.")
        account = self.accounts().get(account_name) if account_name is not None else None
        if account_name is not None and (account is None or account.harness != harness):
            return LaunchOutcome(False, f"No {harness} account named {account_name!r}.")
        if prompt is not None and not (prompt.strip() and len(prompt) <= MESSAGE_LIMIT):
            return LaunchOutcome(False, f"The first message must contain 1 to {MESSAGE_LIMIT} characters.")
        home = account.home if account is not None else None
        if harness == "codex":
            return await self.launch_codex(folder, home, prompt)
        return await self.launch_claude(repository, folder, home, prompt)

    async def launch_codex(self, folder: Path, codex_home: Path | None, prompt: str | None) -> LaunchOutcome:
        bridge = self.live.bridge_for(socket_for_home(codex_home) if codex_home is not None else None)
        if bridge is None or bridge.rpc is None:
            return LaunchOutcome(False, "The Codex bridge is not connected to that Codex app-server.")
        try:
            session = await bridge.start_session(str(folder))
        except (Rejected, RpcError, TransportError) as exc:
            return LaunchOutcome(False, str(exc))
        started = f"Started a Codex session in {folder.name}."
        if prompt is None:
            return LaunchOutcome(True, started)
        try:
            await session.start_turn(prompt, already_in_discord=False)
        except (Rejected, RpcError, TransportError) as exc:
            return LaunchOutcome(False, f"{started} Its first message was not delivered: {exc}")
        return LaunchOutcome(True, f"{started} It is working on the first message.")

    async def launch_claude(self, repository: str, folder: Path, claude_home: Path | None, prompt: str | None) -> LaunchOutcome:
        try:
            result = await self.run(Command(claude_argv(self.find_claude(), prompt), folder, claude_home))
        except TimeoutError:
            return LaunchOutcome(False, f"claude did not finish starting within {CLAUDE_TIMEOUT_SECONDS} seconds.")
        except OSError as exc:
            return LaunchOutcome(False, f"Could not run claude ({type(exc).__name__}).")
        line = first_line(result.output)
        if result.returncode != 0:
            return LaunchOutcome(False, f"claude exited with {result.returncode}: {line or 'no output'}")
        return LaunchOutcome(True, line or f"Started a Claude Code session in {repository}.")

    def claude_homes(self) -> list[Path | None]:
        return [None, *(account.home for account in self.accounts().values() if account.harness == "claude")]

    async def end_claude(self, session_id: object) -> LaunchOutcome:
        short_id = short_session_id(session_id)
        if short_id is None:
            return LaunchOutcome(False, "The end request named no Claude Code session.")
        for claude_home in self.claude_homes():
            command = Command([self.find_claude(), "stop", short_id], Path.home(), claude_home, CLAUDE_STOP_TIMEOUT_SECONDS)
            try:
                result = await self.run(command)
            except TimeoutError:
                return LaunchOutcome(False, f"claude stop did not finish within {CLAUDE_STOP_TIMEOUT_SECONDS} seconds.")
            except OSError as exc:
                return LaunchOutcome(False, f"Could not run claude ({type(exc).__name__}).")
            if result.returncode == 0 and NO_MATCHING_JOB not in result.output:
                return LaunchOutcome(True, first_line(result.output) or f"Stopped Claude Code session {short_id}.")
        return LaunchOutcome(False, STARTED_IN_A_TERMINAL)

    async def outcome(self, message: dict[str, object]) -> LaunchOutcome:
        if message.get("type") == "end":
            return await self.end_claude(message.get("session_id"))
        repository, harness = message.get("repository"), message.get("harness")
        account, prompt = message.get("account"), message.get("message")
        if not isinstance(repository, str) or not isinstance(harness, str):
            return LaunchOutcome(False, "The launch request named no repository or harness.")
        if not isinstance(account, str | None) or not isinstance(prompt, str | None):
            return LaunchOutcome(False, "The launch request had a malformed account or message.")
        return await self.launch(repository, harness, account, prompt)

    async def answer(self, websocket: aiohttp.ClientWebSocketResponse, message: dict[str, object]) -> None:
        kind = "end" if message.get("type") == "end" else "launch"
        outcome = await self.outcome_without_ending_the_bridge(message)
        logger.info("Session launcher %s: %s", kind, outcome.message)
        id_field = f"{kind}_id"
        result = {"type": f"{kind}_result", id_field: message.get(id_field), "ok": outcome.ok, "message": outcome.message}
        try:
            await websocket.send_json(result)
        except (aiohttp.ClientError, ConnectionError) as exc:
            logger.warning("Could not report a session launcher result (%s)", type(exc).__name__)

    async def outcome_without_ending_the_bridge(self, message: dict[str, object]) -> LaunchOutcome:
        # noinspection PyBroadException
        try:
            return await self.outcome(message)
        except Exception:
            logger.exception("Session launcher %s failed", message.get("type"))
            return LaunchOutcome(False, "The request failed on the Mac; see the Codex bridge log.")

    async def serve(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        await websocket.send_json(self.hello())
        async with asyncio.TaskGroup() as requests:
            async for frame in websocket:
                if frame.type in CLOSED_MESSAGES:
                    break
                if frame.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    message = json.loads(frame.data)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("type") in {"launch", "end"}:
                    requests.create_task(self.answer(websocket, message))

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


async def run_launcher(config: BridgeConfig, live: LiveBridges, supervisor_config: Path | None = None) -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=20)) as http:
        await MacLauncher(config, live, supervisor_config).run_forever(http)
