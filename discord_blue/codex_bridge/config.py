from __future__ import annotations

import ipaddress
import os
import socket
import stat
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast
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
    "hello_timeout_seconds": (int, float),
}
TYPE_NAMES = {(str,): "a string", (bool,): "true or false", (int, float): "a number"}


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    server_url: str
    token: str
    socket_path: Path
    host_label: str
    heartbeat_seconds: float = 30
    reconnect_seconds: float = 5
    # How long to wait for hello_ack. Attaching after a Discord Blue restart can take minutes while every session
    # reattaches at once; giving up early only queues another attach behind the ones still running.
    hello_timeout_seconds: float = 300
    # A prompt passed on the command line starts the first turn before stock records it as the thread's preview, so a
    # busy thread with no name or preview yet is read again after each of these delays instead of waiting for its turn
    # to end. Stock records the prompt about 2.5 s after the turn starts.
    unnamed_retry_seconds: tuple[float, ...] = (0.5, 1, 2, 4, 8)


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
        if type(value) not in (expected := FIELD_TYPES[key]):
            raise ValueError(f"{key} must be {TYPE_NAMES[expected]}, not {type(value).__name__}")
    if "hello_timeout_seconds" in raw and not cast(float, raw["hello_timeout_seconds"]) > 0:
        raise ValueError("hello_timeout_seconds must be positive")


def socket_for_home(home: Path) -> Path:
    """Codex transport belongs to CODEX_HOME, independently of its SQLite home."""
    return home.expanduser().absolute() / "app-server-control" / "app-server-control.sock"


def default_socket_path() -> Path:
    home = os.environ.get("CODEX_HOME")
    return socket_for_home(Path(home)) if home else DEFAULT_SOCKET_PATH.expanduser()


def load_config(path: Path) -> BridgeConfig:
    raw = tomllib.loads(path.expanduser().read_text())
    check_types(raw)
    server_url = str(raw.get("server_url") or "")
    validate_server_url(server_url, allow_insecure_ws=raw.get("allow_insecure_ws", False) is True)
    socket_path = Path(str(raw["socket_path"])).expanduser() if raw.get("socket_path") else default_socket_path()
    if not socket_path.is_absolute():
        raise ValueError("socket_path must be absolute")
    token = read_token(raw.get("token_file"))
    if not token:
        raise ValueError(f"set token_file or {TOKEN_ENV} to the Discord Blue agent-session token")
    config = BridgeConfig(
        server_url=server_url,
        token=token,
        socket_path=socket_path,
        host_label=str(raw.get("host_label") or f"Codex on {socket.gethostname().split('.')[0]}"),
    )
    if "hello_timeout_seconds" in raw:
        config = replace(config, hello_timeout_seconds=float(cast(float, raw["hello_timeout_seconds"])))
    return config
