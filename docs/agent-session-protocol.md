# Remote agent session contract

Discord Blue projects agent sessions into Discord threads. The agent harness
manages its process, session state, model calls, approval enforcement, and command
execution. Local clients in this repository connect Codex app-server sessions and
Claude Code channels to the bot. The server does not launch agent processes or
inspect local transcripts; the Claude client reads its own session transcript.

## Connection and identity

Connect to `/agent-session/connect` using a WebSocket with
`Authorization: Bearer <configured token>`. Empty server tokens deny access.
Use TLS or a trusted private transport for deployment. `/health` needs no token.

The client sends JSON text messages. Start each connection with:

```json
{
  "type": "hello",
  "session_id": "stable-agent-thread-id",
  "session_epoch": "current-session-instance-id",
  "host_label": "Codex on example-host",
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

`origin`, `assistant_message`, `title`, and `harness` are optional. `title` is a short
human-readable task name used in the Discord thread name. It is not part of
reconnect matching. `harness` names the agent (`claude` or `codex`); Discord
shows its icon first in the thread name, `<icon> <repo> · <title>`, capped at
100 characters. Without a title the name uses a non-default git branch, and
otherwise just the repository. When another live session would have the same
name, the branch and then a short session ID tell them apart. Launchplane automation
origins use `Auto <repo>#<issue>` instead of a task title, with the harness icon
when supplied. Every `hello`
renames a reused thread whose name is out of date. Older servers ignore
`harness`. Omit `origin` for an interactive
session without an automation request. `session_id` must be stable across
reconnections; `session_epoch` identifies the current running session instance.
The server responds with `{"type":"hello_ack","thread_id":12345,"features":["command_text"]}`
after attaching the Discord thread. `features` lists what the server supports
beyond the base protocol; a client must not rely on a feature the server did not
list, since an older server ignores it and omits `features`. Wait for this acknowledgement before publishing
other events. Send `heartbeat` at an interval shorter than the configured timeout
(default 120 seconds); the timeout counts from `hello_ack`, so a slow attach is
never dropped as silent. A disconnect, a heartbeat timeout or a `hello_ack` the
client stopped waiting for starts a five-minute grace period: the thread is left
exactly as it is (no archive, member change or notice), and a reconnect with the
same `session_id` inside it resumes the thread directly. When the grace period
expires, the bridge posts one "Session ended" line and archives and locks the
thread. A client whose session is really over sends
`{"type":"session_end","session_id":...,"session_epoch":...}` before closing, and
the thread closes the same way at once. Older servers ignore `session_end`.
Reconnect discovery includes archived private threads that the bot has left,
and reattachment restores its membership. The bot needs Discord’s
`Manage Threads` and `Read Message History` permissions for private-thread
recovery. Existing session metadata must still match before a thread is reused.
If Discord denies access to all private archives, discovery falls back to private
archives the bot has joined. Discovery keeps one index of the channel's threads
for every session: it lists every page once and reads each thread's opening
messages once, and a restart's reconnects share one scan. A listing page or read
that fails or is rate limited leaves the index incomplete. The attach refreshes
it a few times (after 0.5, 1, 2 and 4 seconds), and if it is still incomplete the
connection closes without `hello_ack`. A new thread is created only when a
complete index has no match. A new thread is named with a short creation token,
`<name> [k3f9x2]`, until its first rename. Before anything is posted in it, other
empty threads the bot opened under the same token are archived and locked, since
discord.py retries a create that failed with a 5xx and Discord may have made the
first one anyway. A check that cannot finish is retried by maintenance. A matching
thread that turns out to have been
deleted is dropped from the index, and the attach resolves again. Failure to
reopen or rejoin a matching thread closes the connection without `hello_ack`; it
does not create a replacement thread.
If reopening succeeds but joining fails, the thread can remain open without an
attached session until a successful retry or startup cleanup.
Cleanup and reattachment are serialized per session ID so old disconnect cleanup
cannot close a reattached session; unrelated sessions can still attach.
Each session has at most one attach running, and it outlives the connections that
wait for it. A `hello` for a session whose attach is already running joins it
rather than starting over, and waits for the same session's cleanup rather than
being refused. When the attach finishes, only the newest connection gets
`hello_ack`; an older one still waiting is closed. If every waiting connection
has left by then, the session keeps its thread through the disconnect grace
period, so the next `hello` resumes it without searching again. `hello_ack` is
sent once the thread is open and usable: events sent right after it reach the
thread even before Discord's gateway reports the thread reopened.
Heartbeat sweeps skip sessions whose lifecycle lock is busy and try them again on
the next sweep, allowing other stale sessions to be cleaned up.

### Disconnect cleanup and recovery

The connection handler unregisters its session in a finalization path, including
when a late `hello_ack` or event handler fails. Registry removal checks the actual
connection object, so an older connection cannot unregister its replacement.
Closed WebSockets disappear from `/code active` and the health endpoint's
active-session count while cleanup finishes.

Every change to a session thread (reopen, join, member changes, the "Session
ended" notice, archive, leave and rename) goes through that thread's worker. It
sends one request at a time and never cancels one: Discord applies a request
whether or not the bridge is still waiting, and cancelling a discord.py request
during a global rate limit leaves every later request waiting forever. Callers
say whether they want the thread open or closed; after each request the worker
checks again, so a reattach that asks for the thread open while its old archive
is still in flight gets it reopened once that archive lands. Opening comes before
closing, and a rename is sent only when nothing else is pending. A rate limit
longer than 30 seconds is not slept through inside discord.py: the rename is
tried again once Discord allows it. At most four thread requests run at once
across all threads.

WebSocket close, notification cleanup, and thread cleanup have independent wait
budgets: one second for the socket, two seconds for notification cleanup, and
six seconds for thread cleanup. These bound how long teardown waits, not the
requests themselves. A request still running when a wait ends finishes later,
and the shared retry record drops each step as it lands, so a retry never repeats
one. A failed notice or member removal does not stop the archive and leave. The
total nine-second cleanup budget stays below the ten-second reconnect lock wait.
A failed notification operation does not prevent thread cleanup, and a failed
or slow session does not terminate the heartbeat monitor.

Failed Discord cleanup is retained for periodic retry in a bounded, deduplicated
in-memory queue (256 records, up to five retry attempts). Maintenance first runs
after a 20-second startup delay, then every five minutes. For the first ten minutes after a
start, it removes no thread or notification that no session has claimed, because
sessions from before the start may still be reconnecting; it also leaves them
alone while any attach is running. Successful steps are removed
from the shared retry record immediately, so cancellation retains only unfinished
work. Busy attachments, and threads whose worker is still sending a request, defer
retries without consuming their attempt budget;
discovery leaves archived, locked threads alone. Queue overflow and exhausted
retries produce warnings; periodic
orphan discovery provides recovery after records are dropped or the service
restarts. Reconciliation checks
current session/thread ownership before modifying an artifact, preserving threads
adopted by a reconnect. Periodic discovery also recovers orphaned bot-authored
session notifications and threads after a restart; this does not broaden the
reconnect metadata-matching rules or delete conversation history. Background
progress and pending cleanup state are visible in `/health`, with warnings for
failed operations. A dead or stalled monitor is reported as unhealthy.

Reconnect with the same session identity and metadata. Thread recovery matches
persisted session markers; a changed PID can be tolerated only for one matching
stable session ID. The optional assistant snapshot backfills a thread only when
it has no assistant message. The server recognises its own assistant messages by
an invisible trailing marker, which it removes from every other message it
posts, or by the `**Assistant**` label older messages carry. It converts
Markdown tables outside code fences into bullet lists unless that would make
them much longer, and posts at most ten messages per answer. It is not a transcript replay protocol. A new chat
gets a new session ID. The client must reject controls for stale epochs and avoid
executing a repeated command ID twice.

### Client capabilities

A client may include `capabilities` in `hello`: an array of accepted outbound
action names from the Server controls table, plus `approval_decision`. Omission
preserves the legacy full control surface; `[]` allows no remote actions. A
present field must be an array of at most 32 strings, each at most 64 characters.
Malformed values (including null) close the connection before thread lookup.
Unknown strings are ignored, duplicates collapse, and `hello_ack` echoes the
recognized list when capabilities were supplied. Legacy acknowledgements omit it.

Capabilities apply to the current connection. They describe supported features;
they do not grant authorization or replace session admin permission and identity checks.
Discord filters actionable controls, preserves status indicators and local
`/code status`, and checks every outbound action again at dispatch. Unsupported
thread replies are handled with a visible notice and no queued reaction or
command. Unsupported approval/input events produce a generic native-TUI notice
without rendering their payloads or collecting answers. Stale controls cannot
bypass a restricted connection. Connection failures are reported as unconfirmed
delivery; check the native TUI before retrying.

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
| `approval_request` | `approval_id`, `call_id`, `turn_id`, `command` (argv list), `cwd`, optional `reason`, optional `command_text` (only when `hello_ack` lists it): the command exactly as the shell runs it. Discord shows `command_text` verbatim instead of the re-quoted argv, and keeps the request in the native TUI when it is longer than 1,600 characters, contains a code fence, or does not fit one message with its directory. |
| `approval_decision_ack` | `approval_id`: decision submission acknowledged; Discord does not assert the winning outcome. |
| `approval_resolved` | `approval_id`: retire matching approval with neutral Resolved text. |
| `request_user_input_resolved` | `call_id`, `turn_id`: retire the exact matching input prompt with neutral Resolved text. |
| `approval_decision_reject` | `approval_id`, `reason`: decision expired or rejected. |
| `request_user_input` | `call_id`, `turn_id`, `questions`: question objects described below. |
| `title_changed` | `title`: a title learned after `hello`, such as a new name or prompt; the thread is renamed in the background. Discord allows about two renames per thread in ten minutes, so the server renames only when the name changes, keeps only the latest pending name, and waits for the window to reopen. |
| `notice` | `message`: plain text posted in the thread, such as why controls are unavailable. |

Older bridges ignore these unknown resolution, title and notice events.

Resolution events are additive: clients may send them when another subscriber
answers a shared request. No outcome or actor is inferred. Matching requires the
current session epoch and request identity; unknown IDs, stale epochs and duplicate
resolution events are no-ops. Request IDs must be unique within a session epoch;
a replacement prompt uses a new call/approval ID. The server processes inbound
frames serially so request registration precedes its following resolution event.
Prompt UI edits serialize with retirement, and retired input commands cannot
repaint newer prompts when delayed acknowledgements arrive. Capability names
continue to describe accepted outbound actions, not incoming event support.

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

Discord binds each question view to its session epoch, call ID, turn ID, and
Discord message. Replaced views and already-submitted prompts cannot send another
answer or restore a retired prompt through a stale modal. Submit and Cancel
reserve the same prompt before delivery so concurrent clicks send at most once.

Cancellation of a question produces an empty answers object. The client decides
how cancellation resolves its pending tool request. Acknowledge only after local
acceptance; reject unsupported or stale requests with a reason. Keep execution
completion separate from command acceptance.

Approvals use a separate message: `type: "approval_decision"`, `approval_id`,
`session_id`, `session_epoch`, and `decision` (`approved` or `denied`). The
agent retains final approval authority and acknowledges or rejects the decision.
Discord checks session admin permission using the legacy config key
`agent_session.operator_role_name`, falling back to `discord.employee_role_name`.
When both are empty, current code applies no role restriction; see
[#135](https://github.com/cbusillo/discord-blue/issues/135).

## Launchplane provenance

For a client integrating Launchplane automation, map `AGENT_SESSION_ORIGIN` to
`origin.kind`, and `AGENT_SESSION_REQUEST_ID`,
`AGENT_SESSION_REPOSITORY`, `AGENT_SESSION_ISSUE_NUMBER`, and
`AGENT_SESSION_ISSUE_URL` to their corresponding fields. Launchplane emits
`AGENT_SESSION_ORIGIN=launchplane` and `AGENT_SESSION_SOURCE=agent-session`.
Request IDs are opaque and may retain historical prefixes.
The bundled Codex bridge and Claude channel do not populate `origin` from these
environment variables.

## Integration acceptance

Use real Codex and Claude Code sessions to verify hello acknowledgement, mirrored
output, and reconnect without duplicate threads or repeated command execution.
Exercise only each client's advertised controls: Codex supports reply, pause,
status, command approvals and user input; the Claude channel supports status,
plus replies when the channel is enabled, with permission decisions kept in the
terminal. Neither current
client advertises new/end session or autonomous continuation controls. The Server
controls table describes the protocol surface, not a promise that every client
implements it. Unit/transport tests validate the server and local clients; live
integration acceptance is separate.
