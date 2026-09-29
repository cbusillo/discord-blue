"""A fake Discord REST API that the real discord.py HTTP client talks to.

It holds a guild's parent channel, its threads (public or private, archived,
locked, deleted), their messages and members, and serves the routes the
agent-session bridge uses. Requests take a configurable latency and apply at the
end of it, whether or not the client is still waiting: a request cancelled on
the client side still lands, as it does on Discord. Faults are scripted per
route: rate limits (route or global, with `retry_after`) and 5xx responses,
optionally after the request already took effect. Every state change is
reported to a gateway listener after a delay, like a gateway event.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import web

Json = dict[str, Any]
BOT_ID = 999
GUILD_ID = 1
PARENT_ID = 2
EPOCH = datetime(2026, 9, 1, tzinfo=UTC)


@dataclass
class FakeMessage:
    id: int
    channel_id: int
    content: str
    author_id: int = BOT_ID

    def payload(self) -> Json:
        stamp = (EPOCH + timedelta(seconds=self.id)).isoformat()
        author = {"id": str(self.author_id), "username": "bot", "discriminator": "0", "avatar": None}
        return {
            "id": str(self.id),
            "channel_id": str(self.channel_id),
            "author": author,
            "content": self.content,
            "timestamp": stamp,
            "edited_timestamp": None,
            "tts": False,
            "mention_everyone": False,
            "mentions": [],
            "mention_roles": [],
            "attachments": [],
            "embeds": [],
            "pinned": False,
            "type": 0,
        }


@dataclass
class FakeThreadState:
    id: int
    name: str
    private: bool = True
    archived: bool = False
    locked: bool = False
    members: set[int] = field(default_factory=set)
    messages: list[FakeMessage] = field(default_factory=list)
    archived_at: int = 0

    def payload(self) -> Json:
        return {
            "id": str(self.id),
            "type": 12 if self.private else 11,
            "guild_id": str(GUILD_ID),
            "parent_id": str(PARENT_ID),
            "owner_id": str(BOT_ID),
            "name": self.name,
            "member_count": len(self.members),
            "message_count": len(self.messages),
            "thread_metadata": {
                "archived": self.archived,
                "locked": self.locked,
                "auto_archive_duration": 1440,
                "archive_timestamp": (EPOCH + timedelta(seconds=self.archived_at)).isoformat(),
            },
        }


@dataclass
class Fault:
    """Answer the next `times` matching requests with `status`; `applied` lets the request take effect first."""

    method: str
    route: str
    times: int = 1
    status: int = 502
    retry_after: float = 0.0
    is_global: bool = False
    applied: bool = False
    match: Callable[[dict[str, str]], bool] = lambda _ids: True


def respond(payload: object, status: int) -> web.Response:
    # discord.py parses a body as JSON only when Content-Type is exactly application/json, with no charset.
    return web.Response(body=json.dumps(payload).encode(), status=status, headers={"Content-Type": "application/json"})


class FakeDiscord:
    def __init__(self, *, latency: float = 0.0, gateway_delay: float = 0.02) -> None:
        self.latency = latency
        self.route_latency: dict[tuple[str, str], float] = {}
        # Extra latency for requests whose JSON body matches, e.g. only archive edits: (method, route, match, seconds).
        self.body_latency: list[tuple[str, str, Callable[[Json], bool], float]] = []
        self.gateway_delay = gateway_delay
        self.threads: dict[int, FakeThreadState] = {}
        self.deleted: set[int] = set()
        self.parent_messages: list[FakeMessage] = []
        self.faults: list[Fault] = []
        self.requests: list[tuple[str, str]] = []
        self.listeners: list[Callable[[FakeThreadState, bool], None]] = []
        self.ids = itertools.count(10_000)
        self.clock = itertools.count(1)

    # Scenario setup

    def add_thread(
        self,
        name: str,
        *,
        marker: str | None = None,
        private: bool = True,
        archived: bool = False,
        locked: bool = False,
        members: set[int] | None = None,
    ) -> FakeThreadState:
        thread = FakeThreadState(
            id=next(self.ids), name=name, private=private, archived=archived, locked=locked, members=set(members or ())
        )
        if thread.archived:
            thread.archived_at = next(self.clock)
        if marker is not None:
            thread.messages.append(FakeMessage(next(self.ids), thread.id, marker))
        self.threads[thread.id] = thread
        return thread

    def delete_thread(self, thread_id: int) -> None:
        self.deleted.add(thread_id)
        self.threads.pop(thread_id, None)

    # Server

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/api/v10/{path:.*}", self.handle)
        return app

    ROUTES: tuple[tuple[str, str], ...] = (
        ("GET", "/users/@me"),
        ("GET", "/guilds/{guild}/threads/active"),
        ("GET", "/channels/{channel}/threads/archived/public"),
        ("GET", "/channels/{channel}/threads/archived/private"),
        ("GET", "/channels/{channel}/users/@me/threads/archived/private"),
        ("POST", "/channels/{channel}/threads"),
        ("GET", "/channels/{channel}/messages/{message}"),
        ("DELETE", "/channels/{channel}/messages/{message}"),
        ("GET", "/channels/{channel}/messages"),
        ("POST", "/channels/{channel}/messages"),
        ("GET", "/channels/{channel}/thread-members"),
        ("PUT", "/channels/{channel}/thread-members/{member}"),
        ("DELETE", "/channels/{channel}/thread-members/{member}"),
        ("GET", "/channels/{channel}"),
        ("PATCH", "/channels/{channel}"),
    )

    def resolve(self, method: str, path: str) -> tuple[str, dict[str, str]]:
        for route_method, template in self.ROUTES:
            if route_method != method:
                continue
            pattern = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", template) + "$"
            if matched := re.match(pattern, path):
                return template, matched.groupdict()
        raise web.HTTPNotFound(text=f"unsupported route {method} /{path}")

    async def handle(self, request: web.Request) -> web.Response:
        path = "/" + request.match_info["path"]
        template, ids = self.resolve(request.method, path)
        self.requests.append((request.method, template))
        body: Json = await request.json() if request.can_read_body else {}
        # Shielded: a request whose client stops waiting still takes effect, as on Discord.
        work = asyncio.create_task(self.process(request.method, template, ids, dict(request.query), body))
        return await asyncio.shield(work)

    async def process(self, method: str, template: str, ids: dict[str, str], query: dict[str, str], body: Json) -> web.Response:
        delay = self.route_latency.get((method, template), self.latency)
        delay += sum(extra for m, r, match, extra in self.body_latency if m == method and r == template and match(body))
        await asyncio.sleep(delay)
        fault = next((f for f in self.faults if f.method == method and f.route == template and f.match(ids)), None)
        if fault is not None:
            fault.times -= 1
            if fault.times <= 0:
                self.faults.remove(fault)
            if fault.applied:
                self.apply(method, template, ids, query, body)
            if fault.status == 429:
                limit = {"message": "You are being rate limited.", "retry_after": fault.retry_after, "global": fault.is_global}
                # Without a Via header discord.py takes a 429 for a Cloudflare ban and gives up.
                response = respond(limit, 429)
                response.headers["Via"] = "1.1 google"
                return response
            return respond({"message": "fault", "code": 0}, fault.status)
        status, payload = self.apply(method, template, ids, query, body)
        response = web.Response(status=204) if status == 204 else respond(payload, status)
        # Discord's bucket headers; without them discord.py assumes one request at a time per bucket.
        response.headers.update(
            {
                "X-RateLimit-Limit": "50",
                "X-RateLimit-Remaining": "49",
                "X-RateLimit-Reset-After": "1.0",
                "X-RateLimit-Bucket": f"{method}:{template}",
            }
        )
        return response

    def apply(self, method: str, template: str, ids: dict[str, str], query: dict[str, str], body: Json) -> tuple[int, Any]:
        channel_id = int(ids["channel"]) if "channel" in ids else None
        thread = self.threads.get(channel_id) if channel_id is not None else None
        if channel_id is not None and channel_id != PARENT_ID and thread is None:
            return 404, {"message": "Unknown Channel", "code": 10003}
        if template == "/users/@me":
            return 200, {"id": str(BOT_ID), "username": "bot", "discriminator": "0", "avatar": None, "bot": True}
        if template == "/guilds/{guild}/threads/active":
            active = [t.payload() for t in self.threads.values() if not t.archived]
            return 200, {"threads": active, "members": []}
        if template.startswith("/channels/{channel}") and "archived" in template:
            return 200, self.archived_page(template, query)
        if template == "/channels/{channel}/threads":
            created = self.add_thread(str(body.get("name") or "thread"), private=body.get("type", 12) == 12)
            created.members.add(BOT_ID)
            self.notify(created)
            return 201, created.payload()
        messages = self.parent_messages if thread is None else thread.messages
        if template == "/channels/{channel}/messages" and method == "POST":
            message = FakeMessage(next(self.ids), channel_id or PARENT_ID, str(body.get("content") or ""))
            messages.append(message)
            if thread is not None and thread.archived and not thread.locked:
                thread.archived = False  # Posting reopens an archived, unlocked thread.
                self.notify(thread)
            return 200, message.payload()
        if template == "/channels/{channel}/messages":
            return 200, self.history(messages, query)
        if template == "/channels/{channel}/messages/{message}":
            found = next((m for m in messages if m.id == int(ids["message"])), None)
            if found is None:
                return 404, {"message": "Unknown Message", "code": 10008}
            if method == "DELETE":
                messages.remove(found)
                return 204, None
            return 200, found.payload()
        if thread is None:
            return 200, {"id": str(PARENT_ID), "type": 0, "guild_id": str(GUILD_ID), "name": "agent-sessions"}
        if template == "/channels/{channel}/thread-members/{member}":
            member = BOT_ID if ids["member"] == "@me" else int(ids["member"])
            if method == "PUT" and thread.archived:
                return 400, {"message": "Thread is archived", "code": 50083}
            (thread.members.add if method == "PUT" else thread.members.discard)(member)
            return 204, None
        if template == "/channels/{channel}/thread-members":
            return 200, [
                {"id": str(thread.id), "user_id": str(m), "join_timestamp": EPOCH.isoformat(), "flags": 0} for m in thread.members
            ]
        if method == "PATCH":
            if "name" in body:
                thread.name = str(body["name"])
            if "locked" in body:
                thread.locked = bool(body["locked"])
            if "archived" in body:
                if body["archived"] and not thread.archived:
                    thread.archived_at = next(self.clock)
                thread.archived = bool(body["archived"])
            self.notify(thread)
        return 200, thread.payload()

    def archived_page(self, template: str, query: dict[str, str]) -> Json:
        private = template.endswith("/private")
        joined = "/users/@me/" in template
        limit = int(query.get("limit", 50))
        candidates = [
            t for t in self.threads.values() if t.archived and t.private == private and (not joined or BOT_ID in t.members)
        ]
        candidates.sort(key=lambda t: t.archived_at, reverse=True)
        if before := query.get("before"):
            cutoff = datetime.fromisoformat(before)
            candidates = [t for t in candidates if EPOCH + timedelta(seconds=t.archived_at) < cutoff]
        page = candidates[:limit]
        return {"threads": [t.payload() for t in page], "members": [], "has_more": len(candidates) > limit}

    @staticmethod
    def history(messages: list[FakeMessage], query: dict[str, str]) -> list[Json]:
        limit = int(query.get("limit", 50))
        if "after" in query:
            chosen = [m for m in messages if m.id > int(query["after"])][:limit]
            return [m.payload() for m in reversed(chosen)]  # Discord answers newest first.
        newest_first = list(reversed(messages))
        if "before" in query:
            newest_first = [m for m in newest_first if m.id < int(query["before"])]
        return [m.payload() for m in newest_first[:limit]]

    def notify(self, thread: FakeThreadState) -> None:
        """Deliver a THREAD_UPDATE-like event to gateway listeners after the gateway delay."""
        snapshot = FakeThreadState(**{**thread.__dict__, "members": set(thread.members), "messages": list(thread.messages)})
        loop = asyncio.get_running_loop()
        for listener in self.listeners:
            loop.call_later(self.gateway_delay, listener, snapshot, snapshot.id in self.deleted)
