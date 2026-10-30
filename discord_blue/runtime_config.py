import json
import os
from typing import Any


def runtime_overrides() -> dict[str, dict[str, Any]]:
    settings = (
        "DISCORD_BLUE_GUILD_ID",
        "DISCORD_BLUE_BOT_CHANNEL_ID",
        "DISCORD_BLUE_SPAWNED_CHANNEL_ID",
        "DISCORD_BLUE_AUTO_JOIN_USER_IDS",
    )
    if "LAUNCHPLANE_RUNTIME_IDENTITY_JSON" not in os.environ and not any(key in os.environ for key in settings):
        return {}
    for key in settings:
        if not os.environ.get(key, "").strip():
            raise ValueError(f"{key} must be supplied")
    ids = []
    for key in settings[:3]:
        raw = os.environ[key].strip()
        if not raw.isascii() or not raw.isdecimal() or int(raw) <= 0:
            raise ValueError(f"{key} must be a positive ID")
        ids.append(int(raw))
    guild_id, bot_channel_id, spawned_channel_id = ids
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
            "operator_role_name": "Agent Admin",
            "channel_id": bot_channel_id,
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
