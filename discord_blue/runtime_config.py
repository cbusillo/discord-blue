import json
import os
from typing import Any


def _positive_id(key: str, raw: str) -> int:
    raw = raw.strip()
    if not raw.isascii() or not raw.isdecimal() or int(raw) <= 0:
        raise ValueError(f"{key} must be a positive ID")
    return int(raw)


def runtime_overrides() -> dict[str, dict[str, Any]]:
    settings = (
        "DISCORD_BLUE_GUILD_ID",
        "DISCORD_BLUE_BOT_CHANNEL_ID",
        "DISCORD_BLUE_SPAWNED_CHANNEL_ID",
        "DISCORD_BLUE_AUTO_JOIN_USER_IDS",
    )
    optional = ("DISCORD_BLUE_OPERATOR_ROLE_NAME", "DISCORD_BLUE_AGENT_CHANNEL_ID")
    if "LAUNCHPLANE_RUNTIME_IDENTITY_JSON" not in os.environ and not any(key in os.environ for key in (*settings, *optional)):
        return {}
    for key in settings:
        if not os.environ.get(key, "").strip():
            raise ValueError(f"{key} must be supplied")
    guild_id, bot_channel_id, spawned_channel_id = (_positive_id(key, os.environ[key]) for key in settings[:3])
    operator_role = os.environ.get(optional[0]) or "Agent Admin"
    if not operator_role.strip() or operator_role == "from_terminal":
        raise ValueError(f"{optional[0]} must be a nonblank role name other than from_terminal")
    agent_channel_id = _positive_id(optional[1], os.environ.get(optional[1]) or str(bot_channel_id))
    try:
        auto_join = json.loads(os.environ[settings[3]])
    except json.JSONDecodeError:
        raise ValueError(f"{settings[3]} must be a JSON list of positive IDs") from None
    if not isinstance(auto_join, list) or any(type(value) is not int or value <= 0 for value in auto_join):
        raise ValueError(f"{settings[3]} must be a JSON list of positive IDs")
    overrides = {
        "discord": {"guild_id": guild_id, "bot_channel_id": bot_channel_id, "loaded_doodads": ["agent_session_doodad"]},
        "agent_session": {
            "enabled": True,
            "listen_host": "0.0.0.0",
            "listen_port": 8787,
            "operator_role_name": operator_role,
            "channel_id": agent_channel_id,
            "spawned_channel_id": spawned_channel_id,
            "auto_join_user_ids": auto_join,
        },
    }
    for key, section in (("DISCORD_TOKEN", "discord"), ("AGENT_SESSION_TOKEN", "agent_session")):
        value = os.environ.get(key)
        if value is not None:
            value = value.strip()
            if not value or value == "from_terminal":
                raise ValueError(f"{key} must be nonempty")
            overrides[section]["token"] = value
    return overrides
