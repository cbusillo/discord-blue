from __future__ import annotations

import json
import os
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

SERVICE_NAME = "discord-blue"
FALLBACK_VERSION = "0.2.0"
RUNTIME_IDENTITY_ENV = "LAUNCHPLANE_RUNTIME_IDENTITY_JSON"


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
    every_code_enabled: bool = False,
    active_every_code_sessions: int = 0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "service": SERVICE_NAME,
        "status": "ok" if discord_status == "ok" else "unhealthy",
        "version": package_version(),
        "components": {
            "discord": {"status": discord_status},
            "every_code": {
                "status": "ok" if every_code_enabled else "disabled",
                "enabled": every_code_enabled,
                "active_sessions": active_every_code_sessions,
            },
        },
    }
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
