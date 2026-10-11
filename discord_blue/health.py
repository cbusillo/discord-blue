from __future__ import annotations

import json
import math
import os
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

SERVICE_NAME = "discord-blue"
FALLBACK_VERSION = "0.2.0"
RUNTIME_IDENTITY_ENV = "LAUNCHPLANE_RUNTIME_IDENTITY_JSON"


class DiscordGatewayHealth:
    """Keep only cadence and ACK progress, never gateway content or credentials."""

    def __init__(self) -> None:
        self.interval: float | None = None
        self.last_ack: float | None = None
        self.session_started = False

    def disconnect(self) -> None:
        self.interval = None
        self.last_ack = None
        self.session_started = False

    def receive(self, raw: str) -> None:
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(event, dict):
            return
        opcode = event.get("op")
        if opcode == 10:  # HELLO starts a new connection, including resumed sessions.
            self.disconnect()
            data = event.get("d")
            interval = data.get("heartbeat_interval") if isinstance(data, dict) else None
            if isinstance(interval, (int, float)) and not isinstance(interval, bool) and math.isfinite(interval) and interval > 0:
                self.interval = interval / 1000
        elif opcode in (7, 9):  # RECONNECT / INVALID_SESSION must not retain old proof.
            self.disconnect()
        elif opcode == 11 and self.interval is not None:
            self.last_ack = time.monotonic()
        elif opcode == 0 and event.get("t") in ("READY", "RESUMED") and self.interval is not None:
            self.session_started = True

    def evidence(self, *, ready: bool) -> dict[str, Any]:
        age = None if self.last_ack is None else max(0.0, time.monotonic() - self.last_ack)
        # One interval to send the next heartbeat, one to receive its ACK.
        budget = None if self.interval is None else self.interval * 2
        reason = (
            "not_ready"
            if not ready or not self.session_started
            else "unproved"
            if age is None or budget is None
            else "stale"
            if age > budget
            else "recent_ack"
        )
        return {
            "status": "ok" if reason == "recent_ack" else "unhealthy",
            "reason": reason,
            "heartbeat_interval_seconds": self.interval,
            "freshness_budget_seconds": budget,
            "seconds_since_ack": None if age is None else round(age, 3),
        }


def package_version() -> str:
    try:
        return version(SERVICE_NAME)
    except PackageNotFoundError:
        return FALLBACK_VERSION


def env_string(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def source_git_ref() -> str | None:
    return env_string("LAUNCHPLANE_SOURCE_GIT_REF") or env_string("GITHUB_SHA") or env_string("SOURCE_GIT_REF")


def image_reference() -> str | None:
    return env_string("LAUNCHPLANE_IMAGE_REFERENCE") or env_string("IMAGE_REFERENCE")


def runtime_identity() -> dict[str, Any] | None:
    raw_identity = os.environ.get(RUNTIME_IDENTITY_ENV)
    if raw_identity is None or not raw_identity.strip():
        return None
    try:
        parsed_identity = json.loads(raw_identity)
    except json.JSONDecodeError as error:
        return {
            "schema_version": 1,
            "status": "invalid",
            "error": "malformed_json",
            "detail": error.msg,
        }
    if not isinstance(parsed_identity, dict):
        return {
            "schema_version": 1,
            "status": "invalid",
            "error": "expected_json_object",
        }
    return parsed_identity


def health_payload(
    *,
    discord_status: Literal["ok", "unhealthy"],
    discord_gateway: dict[str, Any] | None = None,
    agent_session_enabled: bool = False,
    active_agent_sessions: int = 0,
    disconnected_agent_sessions: int | None = None,
    pending_agent_session_cleanups: int | None = None,
    agent_session_monitor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    monitor_status = agent_session_monitor.get("status") if agent_session_monitor is not None else None
    agent_session_unhealthy = agent_session_enabled and monitor_status in {"dead", "stalled"}
    payload: dict[str, Any] = {
        "schema_version": 1,
        "service": SERVICE_NAME,
        "status": "ok" if discord_status == "ok" and not agent_session_unhealthy else "unhealthy",
        "version": package_version(),
        "components": {
            "discord": {"status": discord_status},
            "agent_session": {
                "status": "unhealthy" if agent_session_unhealthy else "ok" if agent_session_enabled else "disabled",
                "enabled": agent_session_enabled,
                "active_sessions": active_agent_sessions,
            },
        },
    }
    agent_session_component = payload["components"]["agent_session"]
    if discord_gateway is not None:
        payload["components"]["discord"]["gateway"] = discord_gateway
    if disconnected_agent_sessions is not None:
        agent_session_component["disconnected_sessions"] = disconnected_agent_sessions
    if pending_agent_session_cleanups is not None:
        agent_session_component["pending_cleanups"] = pending_agent_session_cleanups
    if agent_session_monitor is not None:
        agent_session_component["monitor"] = agent_session_monitor
    current_source_git_ref = source_git_ref()
    if current_source_git_ref is not None:
        payload["source_git_ref"] = current_source_git_ref
    current_image_reference = image_reference()
    if current_image_reference is not None:
        payload["image_reference"] = current_image_reference
    current_runtime_identity = runtime_identity()
    if current_runtime_identity is not None:
        payload["runtime_identity"] = current_runtime_identity
    return payload
