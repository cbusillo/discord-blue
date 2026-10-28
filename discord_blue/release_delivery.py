from __future__ import annotations

import hashlib
import json
import re
import uuid
import os
from pathlib import Path
from typing import Any

from discord_blue.health import runtime_identity, source_git_ref

HOOK_PATH = Path("claude-plugin/dui/hooks/hooks.json")
BRIDGE_LABEL = "com.shinycomputers.discord-blue-codex-bridge"
BRIDGES = ("discord-blue-codex-bridge", "discord-blue-claude-channel")


def replace_link(path: Path, target: Path) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    try:
        temporary.symlink_to(target)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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
        except json.JSONDecodeError as error:
            raise OSError("Invalid delivery receipt store") from error
        if not isinstance(records, dict) or any(not isinstance(record, dict) for record in records.values()):
            raise OSError("Invalid delivery receipt store")
        return records

    def save(self, host: str, receipt: dict[str, Any]) -> None:
        if str(uuid.UUID(host)) != host:
            raise ValueError("Invalid registered host")
        records = self.read()
        records[host] = receipt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(records))
        temporary.replace(self.path)

    def forget(self, host: str) -> None:
        if str(uuid.UUID(host)) != host:
            raise ValueError("Invalid registered host")
        records = self.read()
        records.pop(host, None)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(records))
        temporary.replace(self.path)

    @staticmethod
    def validate(receipt: dict[str, Any]) -> None:
        if set(receipt) - {"status", "revision", "hooks_sha256", "codex_revision", "codex_pid", "error"}:
            raise ValueError("Unknown receipt fields")
        if receipt.get("status") not in {"pending", "delivered"} or receipt.get("error") not in {
            None,
            "delivery_failed",
            "startup_unverified",
            "source_unavailable",
        }:
            raise ValueError("Invalid receipt state")
        for field, length in (("revision", 40), ("hooks_sha256", 64)):
            if not isinstance(receipt.get(field), str) or re.fullmatch(f"[0-9a-f]{{{length}}}", receipt[field]) is None:
                raise ValueError("Invalid receipt source")
        if "codex_revision" in receipt and (
            not isinstance(receipt["codex_revision"], str) or re.fullmatch(r"[0-9a-f]{40}", receipt["codex_revision"]) is None
        ):
            raise ValueError("Invalid startup revision")
        if "codex_pid" in receipt and (type(receipt["codex_pid"]) is not int or receipt["codex_pid"] <= 0):
            raise ValueError("Invalid startup process")
        if receipt["status"] == "delivered" and (
            not {"codex_revision", "codex_pid"} <= receipt.keys() or receipt.get("error") is not None
        ):
            raise ValueError("Invalid startup receipt")

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
