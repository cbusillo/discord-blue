from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass

from aiohttp import WSMsgType, web

logger = logging.getLogger(__name__)
LAUNCHER_PATH = "/agent-session/launcher"
BUSY_CLOSE_CODE = 4001
HARNESSES = ("codex", "claude")
HELLO_TIMEOUT_SECONDS = 15
LAUNCH_TIMEOUT_SECONDS = 60
AUTOCOMPLETE_LIMIT = 25
ACCOUNT_NAME_LIMIT = 100
MESSAGE_LIMIT = 4000
RESULT_KINDS = {"launch_result": "launch_id", "end_result": "end_id"}
NOT_CONNECTED = "The Mac's session launcher is not connected. Check that the Codex bridge is running."
DISCONNECTED = "The Mac's session launcher disconnected before it answered."


@dataclass(frozen=True, slots=True)
class LauncherAccount:
    name: str
    harness: str


@dataclass(frozen=True, slots=True)
class LauncherHello:
    host_label: str
    repositories: tuple[str, ...]
    harnesses: tuple[str, ...]
    accounts: tuple[LauncherAccount, ...] = ()


@dataclass(frozen=True, slots=True)
class MacAnswer:
    ok: bool
    message: str


def parse_accounts(value: object) -> tuple[LauncherAccount, ...]:
    accounts: list[LauncherAccount] = []
    for entry in value if isinstance(value, list) else []:
        name, harness = (entry.get("name"), entry.get("harness")) if isinstance(entry, dict) else (None, None)
        if isinstance(name, str) and 0 < len(name) <= ACCOUNT_NAME_LIMIT and harness in HARNESSES:
            accounts.append(LauncherAccount(name, harness))
    return tuple(accounts)


def parse_hello(message: object) -> LauncherHello | None:
    if not isinstance(message, dict) or message.get("type") != "launcher_hello":
        return None
    host_label, repositories, harnesses = message.get("host_label"), message.get("repositories"), message.get("harnesses")
    if not isinstance(host_label, str) or not isinstance(repositories, list) or not isinstance(harnesses, list):
        return None
    if not all(isinstance(name, str) for name in repositories) or not all(isinstance(name, str) for name in harnesses):
        return None
    return LauncherHello(
        host_label,
        tuple(sorted(repositories, key=str.lower)),
        tuple(h for h in harnesses if h in HARNESSES),
        parse_accounts(message.get("accounts")),
    )


@dataclass(frozen=True, slots=True)
class PendingRequest:
    websocket: web.WebSocketResponse
    future: asyncio.Future[MacAnswer | None]


def launch_refusal(hello: LauncherHello, repository: str, harness: str, account: str | None, message: str | None) -> str | None:
    if repository not in hello.repositories:
        return f"{hello.host_label} has no repository named {repository!r} under ~/Developer."
    if harness not in hello.harnesses:
        return f"{hello.host_label} cannot start {harness!r} sessions."
    if account is not None and LauncherAccount(account, harness) not in hello.accounts:
        return f"{hello.host_label} has no {harness} account named {account!r}."
    if message is not None and not (message.strip() and len(message) <= MESSAGE_LIMIT):
        return f"The first message must contain 1 to {MESSAGE_LIMIT} characters."
    return None


class SessionLauncher:
    def __init__(self) -> None:
        self.websocket: web.WebSocketResponse | None = None
        self.hello: LauncherHello | None = None
        self.pending: dict[str, PendingRequest] = {}

    def repository_choices(self, typed: str) -> list[str]:
        if self.hello is None:
            return []
        typed = typed.strip().lower()
        return [name for name in self.hello.repositories if typed in name.lower()][:AUTOCOMPLETE_LIMIT]

    def account_choices(self, harness: str | None, typed: str) -> list[str]:
        if self.hello is None:
            return []
        typed = typed.strip().lower()
        names = [account.name for account in self.hello.accounts if harness in (None, account.harness)]
        return [name for name in names if typed in name.lower()][:AUTOCOMPLETE_LIMIT]

    async def serve(self, websocket: web.WebSocketResponse) -> None:
        async with asyncio.timeout(HELLO_TIMEOUT_SECONDS):
            hello = parse_hello(await websocket.receive_json())
        if hello is None:
            return
        if self.websocket is not None and not self.websocket.closed:
            await websocket.close(code=BUSY_CLOSE_CODE, message=b"another launcher is connected")
            return
        self.websocket, self.hello = websocket, hello
        logger.info("Session launcher connected from %s with %s repositories", hello.host_label, len(hello.repositories))
        try:
            async for frame in websocket:
                if frame.type is WSMsgType.TEXT:
                    self.accept_result(websocket, frame.data)
        finally:
            if self.websocket is websocket:
                self.websocket = self.hello = None
            self.abandon_requests(websocket)

    def abandon_requests(self, websocket: web.WebSocketResponse) -> None:
        for request in self.pending.values():
            if request.websocket is websocket and not request.future.done():
                request.future.set_result(None)

    def accept_result(self, websocket: web.WebSocketResponse, data: str) -> None:
        try:
            result = json.loads(data)
        except ValueError:
            return
        kind = result.get("type") if isinstance(result, dict) else None
        if not isinstance(kind, str) or (id_field := RESULT_KINDS.get(kind)) is None:
            return
        request_id = result.get(id_field)
        request = self.pending.get(request_id) if isinstance(request_id, str) else None
        if request is None or request.websocket is not websocket or request.future.done():
            return
        request.future.set_result(MacAnswer(result.get("ok") is True, str(result.get("message") or "")[:1500]))

    async def ask(self, websocket: web.WebSocketResponse, host_label: str, kind: str, fields: dict[str, str]) -> MacAnswer | str:
        request_id = uuid.uuid4().hex
        future: asyncio.Future[MacAnswer | None] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = PendingRequest(websocket, future)
        try:
            await websocket.send_json({"type": kind, f"{kind}_id": request_id, **fields})
            async with asyncio.timeout(LAUNCH_TIMEOUT_SECONDS):
                answer = await future
        except ConnectionError:
            return NOT_CONNECTED
        except TimeoutError:
            return f"{host_label} did not answer within {LAUNCH_TIMEOUT_SECONDS} seconds."
        finally:
            del self.pending[request_id]
        return answer if answer is not None else DISCONNECTED

    async def launch(self, repository: str, harness: str, account: str | None = None, message: str | None = None) -> str:
        websocket, hello = self.websocket, self.hello
        if websocket is None or hello is None or websocket.closed:
            return NOT_CONNECTED
        if refusal := launch_refusal(hello, repository, harness, account, message):
            return refusal
        fields = {"repository": repository, "harness": harness}
        if account is not None:
            fields["account"] = account
        if message is not None:
            fields["message"] = message
        answer = await self.ask(websocket, hello.host_label, "launch", fields)
        if isinstance(answer, str):
            return answer
        if not answer.ok:
            return f"Launch failed: {answer.message}"
        next_step = "" if message is not None else "; type the first message there"
        return f"{answer.message}\nIts thread opens shortly{next_step}."

    async def end(self, session_id: str) -> str:
        websocket, hello = self.websocket, self.hello
        if websocket is None or hello is None or websocket.closed:
            return NOT_CONNECTED
        answer = await self.ask(websocket, hello.host_label, "end", {"session_id": session_id})
        return answer if isinstance(answer, str) else answer.message

    async def close(self) -> None:
        if self.websocket is not None and not self.websocket.closed:
            await self.websocket.close(message=b"bridge shutdown")
