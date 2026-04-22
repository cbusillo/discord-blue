from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(slots=True)
class SessionHello:
    session_id: str
    session_epoch: str
    host_label: str
    cwd: str
    branch: str | None
    pid: int

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SessionHello":
        return cls(
            session_id=str(payload["session_id"]),
            session_epoch=str(payload["session_epoch"]),
            host_label=str(payload.get("host_label") or "Every Code"),
            cwd=str(payload.get("cwd") or ""),
            branch=str(payload["branch"]) if payload.get("branch") else None,
            pid=int(payload.get("pid") or 0),
        )


@dataclass(slots=True)
class RemoteCommand:
    command_id: str
    session_id: str
    session_epoch: str
    kind: Literal["reply", "status_request"]
    text: str | None = None
    issued_by: str | None = None

    def to_message(self) -> dict[str, Any]:
        return {
            "type": "command",
            "command_id": self.command_id,
            "session_id": self.session_id,
            "session_epoch": self.session_epoch,
            "kind": self.kind,
            "text": self.text,
            "issued_by": self.issued_by,
        }
