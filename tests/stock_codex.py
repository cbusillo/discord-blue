"""Disposable stock Codex app-server with synthetic auth and a scripted fake model.

Loopback only: the server runs under ``sandbox-exec`` with outbound network denied
except localhost and Unix sockets. Never point this at a real Codex home.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import shutil
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO

from aiohttp import web

SANDBOX_POLICY = (
    "(version 1) (allow default) (deny network-outbound) "
    '(allow network-outbound (remote ip "localhost:*")) (allow network-outbound (remote unix-socket))'
)
UNLOAD_DELAY_SECONDS = 1
ASK_QUESTIONS = [
    {
        "id": "pick",
        "header": "Pick",
        "question": "Which one?",
        "options": [{"label": "Alpha", "description": "first"}, {"label": "Beta", "description": "second"}],
    }
]


def synthetic_token(account: str) -> str:
    def encode(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    claims = {
        "sub": "synthetic-user",
        "email": "synthetic@example.invalid",
        "exp": 2100000000,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account,
            "chatgpt_user_id": "synthetic-user",
            "chatgpt_plan_type": "plus",
        },
    }
    return f"{encode({'alg': 'none'})}.{encode(claims)}.synthetic"


def model_output(last_user: str, has_tool_output: bool, n: int) -> dict[str, Any]:
    """RUN asks for an escalated command, ASK calls request_user_input, anything else replies."""
    if "RUN" in last_user and not has_tool_output:
        args: dict[str, Any] = {
            "cmd": "touch approved.txt",
            "sandbox_permissions": "require_escalated",
            "justification": "test",
        }
        return {
            "type": "function_call",
            "id": f"fc-{n}",
            "call_id": f"call-{n}",
            "name": "exec_command",
            "arguments": json.dumps(args),
        }
    if "ASK" in last_user and not has_tool_output:
        arguments = json.dumps({"questions": ASK_QUESTIONS})
        return {
            "type": "function_call",
            "id": f"fc-{n}",
            "call_id": f"call-{n}",
            "name": "request_user_input",
            "arguments": arguments,
        }
    return {
        "id": f"m-{n}",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": f"reply {n} to: {last_user[:40]}", "annotations": []}],
    }


class StockCodex:
    def __init__(self, codex_bin: str) -> None:
        self.codex_bin = codex_bin
        self.calls = 0
        self.home = Path(tempfile.mkdtemp(prefix="dbx-", dir="/tmp"))
        self.work = self.home / "work"
        self.socket_path = self.home / "app-server-control" / "app-server-control.sock"
        self.process: asyncio.subprocess.Process | None = None
        self.runner: web.AppRunner | None = None
        self.log: BinaryIO | None = None

    async def model(self, request: web.Request) -> web.StreamResponse:
        if request.path.endswith("/models"):
            return web.json_response({"models": []})
        payload = json.loads(await request.read())
        last_user, has_output = "", False
        for item in payload.get("input", []):
            if item.get("type") == "message" and item.get("role") == "user":
                texts = [c["text"] for c in item.get("content", []) if c.get("type") == "input_text"]
                last_user, has_output = (texts[-1], False) if texts else (last_user, has_output)
            has_output = has_output or item.get("type") == "function_call_output"
        self.calls += 1
        item = model_output(last_user, has_output, self.calls)
        delay = 2.0 if "SLOW" in last_user else 0.01
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        completed = {
            "id": f"r-{self.calls}",
            "status": "completed",
            "output": [item],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }
        for event in (
            {"type": "response.created", "response": {"id": f"r-{self.calls}"}},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": completed},
        ):
            await response.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            await asyncio.sleep(delay)
        with suppress(ConnectionError):
            await response.write_eof()
        return response

    async def __aenter__(self) -> StockCodex:
        app = web.Application()
        token = synthetic_token("test")
        accounts = {
            "accounts": [
                {"id": "test", "workspace_backend_origin": "https://chatgpt.com", "account_routing_override": "NO_CONSTRAINT"}
            ]
        }

        async def refresh(_request: web.Request) -> web.Response:
            return web.json_response({"access_token": token, "id_token": token, "refresh_token": "x"})

        async def check(_request: web.Request) -> web.Response:
            return web.json_response(accounts)

        app.router.add_post("/oauth/token", refresh)
        app.router.add_get("/backend-api/wham/accounts/check", check)
        app.router.add_route("*", "/backend-api/codex/{tail:.*}", self.model)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        backend = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"  # type: ignore[union-attr]
        self.work.mkdir()
        (self.home / "token.py").write_text(f"print({token!r})\n")
        (self.home / "config.toml").write_text(
            # Unload a thread soon after its last client leaves, so closing the TUI is quick to observe.
            f"thread_unload_delay_secs = {UNLOAD_DELAY_SECONDS}\n"
            f'model = "gpt-5.4"\nchatgpt_base_url = "{backend}/backend-api"\ncli_auth_credentials_store = "file"\n'
            'approval_policy = "on-request"\nsandbox_mode = "read-only"\nmodel_provider = "fake"\n'
            f'[model_providers.fake]\nname = "OpenAI"\nbase_url = "{backend}/backend-api/codex"\n'
            'wire_api = "responses"\nsupports_websockets = false\n'
            f'[model_providers.fake.auth]\ncommand = "{sys.executable}"\nargs = ["{self.home / "token.py"}"]\n'
            "timeout_ms = 5000\nrefresh_interval_ms = 1\n"
            "[analytics]\nenabled = false\n[feedback]\nenabled = false\n[features]\nremote_control = false\napps = false\n"
        )
        tokens = {"access_token": token, "id_token": token, "refresh_token": "x", "account_id": "test"}
        now = datetime.datetime.now(datetime.UTC).isoformat()
        (self.home / "auth.json").write_text(
            json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": tokens, "last_refresh": now})
        )
        (self.home / "auth.json").chmod(0o600)
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.home),
            "CODEX_HOME": str(self.home),
            "CODEX_SQLITE_HOME": str(self.home / "shared-sqlite"),
            "CODEX_REFRESH_TOKEN_URL_OVERRIDE": f"{backend}/oauth/token",
        }
        self.log = log = (self.home / "app-server.log").open("wb")
        self.process = await asyncio.create_subprocess_exec(
            "/usr/bin/sandbox-exec", "-p", SANDBOX_POLICY, self.codex_bin, "app-server", "--listen", "unix://",
            cwd=self.work, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log,
        )  # fmt: skip
        for _ in range(400):
            if self.socket_path.exists():
                return self
            await asyncio.sleep(0.05)
        await self.__aexit__()
        raise RuntimeError("stock app-server did not create its socket")

    async def __aexit__(self, *_args: object) -> None:
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.runner is not None:
            await self.runner.cleanup()
        if self.log is not None:
            self.log.close()
        shutil.rmtree(self.home, ignore_errors=True)
