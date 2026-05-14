from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(slots=True)
class SessionOrigin:
    kind: str
    request_id: str | None
    repository: str | None
    issue_number: int | None
    issue_url: str | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SessionOrigin":
        issue_number = payload.get("issue_number")
        try:
            parsed_issue_number = int(issue_number) if issue_number is not None else None
        except (TypeError, ValueError):
            parsed_issue_number = None
        return cls(
            kind=str(payload.get("kind") or ""),
            request_id=str(payload["request_id"]) if payload.get("request_id") else None,
            repository=str(payload["repository"]) if payload.get("repository") else None,
            issue_number=parsed_issue_number,
            issue_url=str(payload["issue_url"]) if payload.get("issue_url") else None,
        )


@dataclass(slots=True)
class SessionHello:
    session_id: str
    session_epoch: str
    host_label: str
    cwd: str
    branch: str | None
    pid: int
    origin: SessionOrigin | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SessionHello":
        origin_payload = payload.get("origin")
        origin = SessionOrigin.from_payload(origin_payload) if isinstance(origin_payload, dict) else None
        return cls(
            session_id=str(payload["session_id"]),
            session_epoch=str(payload["session_epoch"]),
            host_label=str(payload.get("host_label") or "Every Code"),
            cwd=str(payload.get("cwd") or ""),
            branch=str(payload["branch"]) if payload.get("branch") else None,
            pid=int(payload.get("pid") or 0),
            origin=origin,
        )


@dataclass(slots=True)
class SessionMetadataChanged:
    session_id: str
    session_epoch: str
    cwd: str | None
    branch: str | None
    reason: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SessionMetadataChanged":
        return cls(
            session_id=str(payload["session_id"]),
            session_epoch=str(payload["session_epoch"]),
            cwd=str(payload["cwd"]) if payload.get("cwd") else None,
            branch=str(payload["branch"]) if payload.get("branch") else None,
            reason=str(payload.get("reason") or ""),
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
            assistant_message=(str(payload["assistant_message"]) if payload.get("assistant_message") is not None else None),
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
class RequestUserInputQuestionOption:
    label: str
    description: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RequestUserInputQuestionOption":
        return cls(
            label=str(payload.get("label") or ""),
            description=str(payload.get("description") or ""),
        )


@dataclass(slots=True)
class RequestUserInputQuestion:
    id: str
    header: str
    question: str
    is_other: bool
    is_secret: bool
    options: list[RequestUserInputQuestionOption]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RequestUserInputQuestion":
        options_payload = payload.get("options")
        options = []
        if isinstance(options_payload, list):
            options = [RequestUserInputQuestionOption.from_payload(option) for option in options_payload if isinstance(option, dict)]
        return cls(
            id=str(payload.get("id") or ""),
            header=str(payload.get("header") or ""),
            question=str(payload.get("question") or ""),
            is_other=bool(payload.get("isOther") or False),
            is_secret=bool(payload.get("isSecret") or False),
            options=options,
        )


@dataclass(slots=True)
class RemoteRequestUserInput:
    call_id: str
    turn_id: str
    session_id: str
    session_epoch: str
    questions: list[RequestUserInputQuestion]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RemoteRequestUserInput":
        questions_payload = payload.get("questions")
        questions = []
        if isinstance(questions_payload, list):
            questions = [
                RequestUserInputQuestion.from_payload(question) for question in questions_payload if isinstance(question, dict)
            ]
        return cls(
            call_id=str(payload.get("call_id") or ""),
            turn_id=str(payload.get("turn_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            session_epoch=str(payload.get("session_epoch") or ""),
            questions=questions,
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
    kind: Literal[
        "reply",
        "continue_autonomously",
        "pause_current_turn",
        "new_session",
        "end_session",
        "request_user_input_response",
        "status_request",
    ]
    text: str | None = None
    call_id: str | None = None
    turn_id: str | None = None
    response: dict[str, Any] | None = None
    issued_by: str | None = None

    def to_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "type": "command",
            "command_id": self.command_id,
            "session_id": self.session_id,
            "session_epoch": self.session_epoch,
            "kind": self.kind,
            "text": self.text,
            "issued_by": self.issued_by,
        }
        if self.call_id is not None:
            message["call_id"] = self.call_id
        if self.turn_id is not None:
            message["turn_id"] = self.turn_id
        if self.response is not None:
            message["response"] = self.response
        return message
