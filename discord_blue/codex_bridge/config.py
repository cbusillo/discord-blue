from __future__ import annotations

import ipaddress
import os
import socket
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_CONFIG_PATH = Path("~/.config/discord-blue/codex-bridge.toml")
DEFAULT_SOCKET_PATH = Path("~/.codex/app-server-control/app-server-control.sock")
TOKEN_ENV = "AGENT_SESSION_TOKEN"
# Each key's TOML type. A quoted "false" is a string, not a boolean, so it is rejected, not truthy.
FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "server_url": (str,),
    "token_file": (str,),
    "socket_path": (str,),
    "host_label": (str,),
    "allow_insecure_ws": (bool,),
    "idle_release_hours": (int, float),
}
TYPE_NAMES = {str: "a string", bool: "true or false", int: "a number", float: "a number"}


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    server_url: str
    token: str
    socket_path: Path
    host_label: str
    idle_release_seconds: float = 12 * 3600
    heartbeat_seconds: float = 30
    reconnect_seconds: float = 5
    hello_timeout_seconds: float = 90


def validate_server_url(url: str, *, allow_insecure_ws: bool) -> None:
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != "/agent-session/connect":
        raise ValueError("server_url must be a credential-free /agent-session/connect URL without query or fragment")
    if not parsed.hostname:
        raise ValueError("server_url needs a host")
    if parsed.scheme == "wss":
        return
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = False
    if parsed.scheme != "ws" or not (loopback or allow_insecure_ws):
        raise ValueError("server_url must use wss; ws needs a loopback address or allow_insecure_ws on a trusted private network")


def read_token(token_file: str | None) -> str:
    if token_file is None:
        return os.environ.get(TOKEN_ENV, "").strip()
    path = Path(token_file).expanduser()
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("token_file must not be readable by other users")
    return path.read_text().strip()


def check_types(raw: dict[str, object]) -> None:
    if unknown := sorted(set(raw) - set(FIELD_TYPES)):
        raise ValueError(f"unknown config keys: {', '.join(unknown)}")
    for key, value in raw.items():
        expected = FIELD_TYPES[key]
        # bool is an int subclass; only a key that expects a boolean accepts one.
        if not isinstance(value, expected) or (isinstance(value, bool) and bool not in expected):
            raise ValueError(f"{key} must be {TYPE_NAMES[expected[0]]}, not {type(value).__name__}")


def load_config(path: Path) -> BridgeConfig:
    raw = tomllib.loads(path.expanduser().read_text())
    check_types(raw)
    server_url = str(raw.get("server_url") or "")
    validate_server_url(server_url, allow_insecure_ws=raw.get("allow_insecure_ws", False) is True)
    token = read_token(raw.get("token_file"))
    if not token:
        raise ValueError(f"set token_file or {TOKEN_ENV} to the Discord Blue agent-session token")
    return BridgeConfig(
        server_url=server_url,
        token=token,
        socket_path=Path(str(raw.get("socket_path") or DEFAULT_SOCKET_PATH)).expanduser(),
        host_label=str(raw.get("host_label") or f"Codex on {socket.gethostname().split('.')[0]}"),
        idle_release_seconds=float(raw.get("idle_release_hours", 12)) * 3600,
    )
