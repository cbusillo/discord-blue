"""Optional server inputs delivered by the runtime, separate from local TOML."""

import os
import tomllib
from typing import Any, get_args, get_origin, get_type_hints


def _matches_type(value: object, expected: object) -> bool:
    if get_origin(expected) is list:
        (item_type,) = get_args(expected)
        return isinstance(value, list) and all(_matches_type(item, item_type) for item in value)
    return type(value) is expected


def runtime_overrides(sections: dict[str, object]) -> dict[str, dict[str, Any]]:
    """Validate every input before returning overrides; never include values in errors."""
    overrides: dict[str, dict[str, Any]] = {}
    raw = os.environ.get("DISCORD_BLUE_RUNTIME_CONFIG")
    if raw is None:
        return overrides
    try:
        parsed = tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        raise ValueError("DISCORD_BLUE_RUNTIME_CONFIG must be valid non-secret TOML") from None
    if not parsed:
        raise ValueError("DISCORD_BLUE_RUNTIME_CONFIG must contain non-secret settings")
    for section, values in parsed.items():
        if section not in sections or not isinstance(values, dict):
            raise ValueError("Unsupported runtime configuration section")
        types = get_type_hints(type(sections[section]))
        for key, value in values.items():
            if key == "token" or key not in types or not _matches_type(value, types[key]):
                raise ValueError("Unsupported runtime configuration field or type")
        overrides[section] = values
    for env_key, section in (("DISCORD_TOKEN", "discord"), ("AGENT_SESSION_TOKEN", "agent_session")):
        value = os.environ.get(env_key)
        if value is not None:
            if not value.strip() or value == "from_terminal":
                raise ValueError(f"{env_key} must be nonempty")
            overrides.setdefault(section, {})["token"] = value
    return overrides
