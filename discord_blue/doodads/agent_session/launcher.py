from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass

from aiohttp import WSMsgType, web

logger = logging.getLogger(__name__)
LAUNCHER_PATH = "/agent-session/launcher"
REPLACED_CLOSE_CODE = 4001
HARNESSES = ("codex", "claude")
HELLO_TIMEOUT_SECONDS = 15
LAUNCH_TIMEOUT_SECONDS = 60
AUTOCOMPLETE_LIMIT = 25
NOT_CONNECTED = "The Mac's session launcher is not connected. Check that the Codex bridge is running."


@dataclass(frozen=True, slots=True)
class LauncherHello:
    host_label: str
    repositories: tuple[str, ...]
    harnesses: tuple[str, ...]


def parse_hello(message: object) -> LauncherHello | None:
    if not isinstance(message, dict) or message.get("type") != "launcher_hello":
        return None
    host_label, repositories, harnesses = message.get("host_label"), message.get("repositories"), message.get("harnesses")
    if not isinstance(host_label, str) or not isinstance(repositories, list) or not isinstance(harnesses, list):
        return None
    if not all(isinstance(name, str) for name in repositories) or not all(isinstance(name, str) for name in harnesses):
        return None
    return LauncherHello(host_label, tuple(sorted(repositories, key=str.lower)), tuple(h for h in harnesses if h in HARNESSES))


@dataclass(frozen=True, slots=True)
class PendingLaunch:
    websocket: web.WebSocketResponse
    future: asyncio.Future[str]


class SessionLauncher:
    def __init__(self) -> None:
        self.websocket: web.WebSocketResponse | None = None
        self.hello: LauncherHello | None = None
        self.pending: dict[str, PendingLaunch] = {}

    def repository_choices(self, typed: str) -> list[str]:
        if self.hello is None:
            return []
        typed = typed.strip().lower()
        return [name for name in self.hello.repositories if typed in name.lower()][:AUTOCOMPLETE_LIMIT]

    async def serve(self, websocket: web.WebSocketResponse) -> None:
        async with asyncio.timeout(HELLO_TIMEOUT_SECONDS):
            hello = parse_hello(await websocket.receive_json())
        if hello is None:
            return
        await self.replace_connection(websocket, hello)
        try:
            async for frame in websocket:
                if frame.type is WSMsgType.TEXT:
                    self.accept_result(websocket, frame.data)
        finally:
            if self.websocket is websocket:
                self.websocket = self.hello = None
            self.abandon_launches(websocket)

    def abandon_launches(self, websocket: web.WebSocketResponse) -> None:
        for launch in self.pending.values():
            if launch.websocket is websocket and not launch.future.done():
                launch.future.set_result("The Mac's session launcher disconnected before reporting the launch.")

    async def replace_connection(self, websocket: web.WebSocketResponse, hello: LauncherHello) -> None:
        previous = self.websocket
        self.websocket, self.hello = websocket, hello
        logger.info("Session launcher connected from %s with %s repositories", hello.host_label, len(hello.repositories))
        if previous is not None and not previous.closed:
            await previous.close(code=REPLACED_CLOSE_CODE, message=b"replaced by a newer launcher")

    def accept_result(self, websocket: web.WebSocketResponse, data: str) -> None:
        try:
            result = json.loads(data)
        except ValueError:
            return
        if not isinstance(result, dict) or result.get("type") != "launch_result":
            return
        launch_id = result.get("launch_id")
        launch = self.pending.get(launch_id) if isinstance(launch_id, str) else None
        if launch is None or launch.websocket is not websocket or launch.future.done():
            return
        text = str(result.get("message") or "")[:1500]
        started = f"{text}\nIts thread opens shortly; type the first message there."
        launch.future.set_result(started if result.get("ok") is True else f"Launch failed: {text}")

    async def launch(self, repository: str, harness: str) -> str:
        websocket, hello = self.websocket, self.hello
        if websocket is None or hello is None or websocket.closed:
            return NOT_CONNECTED
        if repository not in hello.repositories:
            return f"{hello.host_label} has no repository named {repository!r} under ~/Developer."
        if harness not in hello.harnesses:
            return f"{hello.host_label} cannot start {harness!r} sessions."
        launch_id = uuid.uuid4().hex
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.pending[launch_id] = PendingLaunch(websocket, future)
        try:
            await websocket.send_json({"type": "launch", "launch_id": launch_id, "repository": repository, "harness": harness})
            async with asyncio.timeout(LAUNCH_TIMEOUT_SECONDS):
                return await future
        except ConnectionError:
            return NOT_CONNECTED
        except TimeoutError:
            return f"{hello.host_label} did not report the launch within {LAUNCH_TIMEOUT_SECONDS} seconds."
        finally:
            del self.pending[launch_id]

    async def close(self) -> None:
        if self.websocket is not None and not self.websocket.closed:
            await self.websocket.close(message=b"bridge shutdown")
