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
class SessionStatus:
    session_id: str
    session_epoch: str
    message: str | None
    assistant_message: str | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SessionStatus":
        return cls(
            session_id=str(payload["session_id"]),
            session_epoch=str(payload["session_epoch"]),
            message=str(payload["message"]) if payload.get("message") is not None else None,
            assistant_message=(
                str(payload["assistant_message"])
                if payload.get("assistant_message") is not None
                else None
            ),
        )


@dataclass(slots=True)
class UserMessage:
    session_id: str
    session_epoch: str
    message: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "UserMessage":
        return cls(
            session_id=str(payload["session_id"]),
            session_epoch=str(payload["session_epoch"]),
            message=str(payload.get("message") or ""),
        )


@dataclass(slots=True)
class RemoteApprovalRequest:
    approval_id: str
    call_id: str
    turn_id: str
    session_id: str
    session_epoch: str
    command: list[str]
    cwd: str
    reason: str | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RemoteApprovalRequest":
        command = payload.get("command")
        if not isinstance(command, list):
            command = []
        return cls(
            approval_id=str(payload["approval_id"]),
            call_id=str(payload.get("call_id") or ""),
            turn_id=str(payload.get("turn_id") or ""),
            session_id=str(payload["session_id"]),
            session_epoch=str(payload["session_epoch"]),
            command=[str(part) for part in command],
            cwd=str(payload.get("cwd") or ""),
            reason=str(payload["reason"]) if payload.get("reason") is not None else None,
        )


@dataclass(slots=True)
class RemoteApprovalDecision:
    approval_id: str
    session_id: str
    session_epoch: str
    decision: Literal["approved", "denied"]

    def to_message(self) -> dict[str, Any]:
        return {
            "type": "approval_decision",
            "approval_id": self.approval_id,
            "session_id": self.session_id,
            "session_epoch": self.session_epoch,
            "decision": self.decision,
        }


@dataclass(slots=True)
class RemoteCommand:
    command_id: str
    session_id: str
    session_epoch: str
    kind: Literal["reply", "continue_autonomously", "status_request"]
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
