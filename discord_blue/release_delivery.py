from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from discord_blue.health import runtime_identity, source_git_ref

HOOK_PATH = Path("claude-plugin/dui/hooks/hooks.json")
BRIDGE_LABEL = "com.shinycomputers.discord-blue-codex-bridge"
BRIDGES = ("discord-blue-codex-bridge", "discord-blue-claude-channel")


def production_delivery() -> dict[str, str]:
    identity = runtime_identity() or {}
    revision = identity.get("source_git_ref", "")
    if (
        identity.get("product") != "discord-blue"
        or identity.get("instance") != "prod"
        or identity.get("environment_kind", "stable") != "stable"
        or not identity.get("deployment_record_id")
        or not identity.get("artifact_id")
        or not isinstance(revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", revision) is None
        or revision != source_git_ref()
    ):
        raise ValueError("Verified production source is unavailable")
    hooks = Path(__file__).resolve().parent.parent / HOOK_PATH
    return {"revision": revision, "hooks_sha256": hashlib.sha256(hooks.read_bytes()).hexdigest()}


class DeliveryReceipts:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, dict[str, Any]]:
        try:
            records = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        if not isinstance(records, dict) or any(not isinstance(record, dict) for record in records.values()):
            raise ValueError("Invalid delivery receipt store")
        return records

    def save(self, host: str, receipt: dict[str, Any]) -> None:
        if re.fullmatch(r"[0-9a-f-]{36}", host) is None:
            raise ValueError("Invalid registered host")
        records = self.read()
        records[host] = receipt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(records))
        temporary.replace(self.path)

    def status(self, target: dict[str, str]) -> dict[str, Any]:
        records = self.read()
        clients = {
            host: {
                "status": "delivered" if self.matches(receipt, target) else "pending",
                "receipt": receipt,
            }
            for host, receipt in records.items()
        }
        pending = sum(client["status"] == "pending" for client in clients.values())
        return {"status": "pending" if pending or not clients else "delivered", "target": target, "clients": clients}

    @staticmethod
    def matches(receipt: dict[str, Any], target: dict[str, str]) -> bool:
        return (
            receipt.get("status") == "delivered"
            and receipt.get("revision") == target["revision"]
            and receipt.get("hooks_sha256") == target["hooks_sha256"]
            and receipt.get("codex_revision") == target["revision"]
            and type(receipt.get("codex_pid")) is int
            and receipt["codex_pid"] > 0
        )
