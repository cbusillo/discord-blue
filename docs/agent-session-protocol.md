# Remote agent session contract

Discord Blue projects agent sessions into Discord threads. Codex Lab owns the
agent process, session state, model calls, approval enforcement, and command
execution. The bridge does not launch processes or inspect agent rollout files.

## Connection and identity

Connect to `/agent-session/connect` using a WebSocket with
`Authorization: Bearer <configured token>`. Empty server tokens deny access.
Use TLS or a trusted private transport for deployment. `/health` needs no token.
The retired `/every-code/connect` endpoint is absent.

The client sends JSON text messages. Start each connection with:

```json
{
  "type": "hello",
  "session_id": "stable-agent-thread-id",
  "session_epoch": "current-session-instance-id",
  "host_label": "Codex Lab",
  "cwd": "/workspace/example",
  "branch": "feature/example",
  "pid": 123,
  "assistant_message": "Optional last completed assistant answer",
  "origin": {
    "kind": "launchplane",
    "request_id": "opaque-work-request-id",
    "repository": "example/project",
    "issue_number": 42,
    "issue_url": "https://github.com/example/project/issues/42"
  }
}
```

`origin` and `assistant_message` are optional. Omit `origin` for an interactive
session without an automation request. `session_id` must be stable across
reconnections; `session_epoch` identifies the current running session instance.
The server responds with `{"type":"hello_ack","thread_id":12345}` after
attaching the Discord thread. Wait for this acknowledgement before publishing
other events. Send `heartbeat` at an interval shorter than the configured timeout
(default 120 seconds). On disconnect or timeout the bridge archives the thread.

Reconnect with the same session identity and metadata. Thread recovery matches
persisted session markers; a changed PID can be tolerated only for one matching
stable session ID. The optional assistant snapshot backfills a thread only when
it has no assistant message. It is not a transcript replay protocol. A new chat
gets a new session ID. The client must reject controls for stale epochs and avoid
executing a repeated command ID twice.

## Client events

All session events carry `session_id` and `session_epoch`. Field shapes live in
[protocol.py](../discord_blue/doodads/agent_session/protocol.py).
The bridge ignores events before `hello`, from a replaced connection, or with
an ID or epoch that differs from the connection's current session.

| Type | Additional fields / behavior |
| --- | --- |
| `heartbeat` | Keeps the connected session alive. |
| `user_message` | `message`: text entered in the local session. |
| `status_changed` | `message`: status text; `assistant_message` is optional. |
| `turn_complete` | `message`, `assistant_message`: completed answer mirrored to Discord. |
| `error` | `message`: user-visible error. |
| `command_ack` | `command_id`: accepted for execution, not proof of completion. |
| `command_reject` | `command_id`, `reason`: command could not be accepted. |
| `approval_request` | `approval_id`, `call_id`, `turn_id`, `command` (argv list), `cwd`, optional `reason`. |
| `approval_decision_ack` | `approval_id`: decision accepted by the agent. |
| `approval_decision_reject` | `approval_id`, `reason`: decision expired or rejected. |
| `request_user_input` | `call_id`, `turn_id`, `questions`: question objects described below. |

Question objects carry `id`, `header`, `question`, `isOther`, `isSecret`, and
`options` (objects with `label` and `description`). Do not send secrets through
Discord; the existing form is not a secret-entry channel. The Discord form
supports up to four questions and 25 select options per question, with one
option reserved when Other is enabled. Keep client requests within those bounds.

## Server controls

A command has `type: "command"`, `command_id`, `session_id`, `session_epoch`,
`kind`, optional `text`, and `issued_by` (Discord user ID). Supported kinds:

| Kind | Client action |
| --- | --- |
| `reply` | Deliver `text` to the active session. |
| `continue_autonomously` | Continue until user involvement is required. |
| `pause_current_turn` | Interrupt the current turn. |
| `new_session` | Start a fresh chat in the same working directory. |
| `end_session` | End/disconnect this session. |
| `status_request` | Publish current session status. |
| `request_user_input_response` | Resolve `call_id` / `turn_id` using `response.answers`, mapping question IDs to `{"answers":["text"]}`. |

Cancellation of a question produces an empty answers object. The client decides
how cancellation resolves its pending tool request. Acknowledge only after local
acceptance; reject unsupported or stale requests with a reason. Keep execution
completion separate from command acceptance.

Approvals use a separate message: `type: "approval_decision"`, `approval_id`,
`session_id`, `session_epoch`, and `decision` (`approved` or `denied`). The
agent retains final approval authority and acknowledges or rejects the decision.
Discord operator-role checks restrict who can send replies and controls.

## Launchplane provenance

Map `AGENT_SESSION_ORIGIN` to `origin.kind`, and `AGENT_SESSION_REQUEST_ID`,
`AGENT_SESSION_REPOSITORY`, `AGENT_SESSION_ISSUE_NUMBER`, and
`AGENT_SESSION_ISSUE_URL` to their corresponding fields. Launchplane emits
`AGENT_SESSION_ORIGIN=launchplane` and `AGENT_SESSION_SOURCE=agent-session`.
Request IDs are opaque and may retain historical prefixes.

## Integration acceptance

Use a real Codex Lab session to verify hello acknowledgement, mirrored output,
reply, pause, new/end session, status, approvals, user input, and reconnect
without duplicate threads or command execution. Unit/transport tests in this
repository validate the server; they do not prove a Codex Lab client is shipped.
