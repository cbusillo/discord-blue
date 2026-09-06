# Codex Lab development adapter

`codex-lab-discord-dev` connects one existing **daemon-backed native TUI
conversation** to Discord Blue. It runs on the Codex Lab host and requires no
Codex Lab source changes. It is opt-in development tooling; the bot does not
start it automatically. Launchplane worker launch/auth wiring remains separate.

## Start

Use a Codex Lab build that supports the app-server v2 Unix transport,
`thread/resume.initialTurnsPage`, and `thread.canAcceptDirectInput`. The local
development build from source `ef0b790f4df0a5e6c8c69d6758bc289e081646da` was used
to qualify shared native TUI control with a mock model. This is compatibility
evidence, not a required version pin or an installed release claim.

1. Start Codex Lab's app-server on a private Unix socket using the daemon's
   intended home, authentication, and configuration:

   ```sh
   codex-lab app-server --listen unix:///absolute/private/path/lab.sock
   ```

2. Start the native TUI against that same daemon:

   ```sh
   codex-lab --remote unix:///absolute/private/path/lab.sock
   ```

3. Complete a first turn and obtain the exact root conversation ID from the
   TUI's session information. An empty conversation has no resumable rollout.
   An already running embedded TUI cannot be attached through a socket; resume
   its saved conversation in the daemon-backed TUI first. To resume an existing
   materialized conversation:

   ```sh
   codex-lab --remote unix:///absolute/private/path/lab.sock resume THREAD_ID
   ```

4. Supply the configured Discord Blue **agent-session bridge token**, not the
   Discord bot token, in `AGENT_SESSION_TOKEN` through your normal secret
   environment mechanism. From this repository run:

   ```sh
   uv run codex-lab-discord-dev \
     --socket /absolute/private/path/lab.sock \
     --thread-id THREAD_ID \
     --bridge-url wss://YOUR_BRIDGE_HOST/agent-session/connect
   ```

   `--token-env NAME` selects another environment variable. Tokens are never
   command-line arguments. The URL must have the exact endpoint path and no
   embedded credentials, query, or fragment. Unencrypted `ws` is accepted only
   for literal loopback IPs in local tests. The Unix socket must belong to the
   current user and must not be writable by other users.

Keep the native TUI open: it owns approval and input decisions. Do not simply
add `--remote` to a Launchplane workload-identity launch. Codex Lab requires that
identity on the app-server host; selecting it on the TUI currently forces
embedded mode. Production wiring must follow Launchplane's authorization plan.

## Behavior

| Discord action | Development adapter behavior |
| --- | --- |
| Thread reply | Starts a turn when idle; steers the observed active turn using its ID. |
| Pause | Interrupts the observed active turn. A changed/finished turn is rejected. |
| Status | Checks app-server responsiveness and reports the last observed status. |
| End session | Detaches this adapter and archives its Discord connection. The native TUI, daemon, and active turn continue. |
| New session / continue autonomously | Rejected with an instruction to use the native TUI. |
| Approval / request-user-input | Remain local to the native TUI; Discord receives only a generic action-required status. |

Completed assistant answers are mirrored once per observed turn. Streaming
deltas, tool output, and prompts typed locally in the TUI are not posted.
Answers over 32,000 characters are truncated with a notice. Existing history is
used only for initial backfill, not reposted as new completions. Backfill can be
empty when the latest turn contains no supported text answer. The adapter uses
the actual thread cwd/branch and its own PID for connection metadata; it emits
no Launchplane provenance.

Approval and input payloads, including secret prompts, are not forwarded. The
adapter never answers an app-server approval request. App-server sends those
requests to all subscribers and the first response wins; its resolution event
does not identify the winning client or decision. A future remote approval UI
needs neutral resolution semantics before it can attribute decisions honestly.

## Recovery and limits

Discord reconnects retain the same thread ID, epoch, and command deduplication
cache. Completions received while disconnected remain in a bounded in-memory
queue and are sent after `hello_ack`. A send that fails has an ambiguous
delivery result and is not replayed. The protocol has no event delivery receipt,
so loss at that boundary is possible; consult the native TUI for authoritative
history. A process restart also loses that in-memory output queue.

A new process gets a new epoch and rejects old controls. Duplicate command IDs
never execute twice within an epoch; reusing an ID with changed content is
rejected. After 10,000 commands, new commands are rejected until an explicit
restart. Bounded backlog/event limits stop the adapter rather than silently
evicting deduplication state. Restart with the same conversation ID to recover
its Discord thread; no rollout-file or process scraping is used.

App-server disconnect, protocol failure, or RPC timeout stops the adapter. An
uncertain command is never automatically retried. Check the native TUI before
sending it again. Heartbeats require a successful app-server read, preventing
the adapter from reporting a disconnected daemon as healthy. Authentication or
endpoint rejection is terminal; transient Discord transport loss reconnects.
Discord writes are serialized and time-bounded, and a connection closed before
the initial acknowledgement is retried. Malformed acknowledgement frames stop
the adapter with a bounded diagnostic.

## Verification

```sh
uv run python -m unittest tests.test_codex_lab_rpc tests.test_codex_lab_adapter -q
uv run python -m unittest discover -s tests -q
uv run mypy .
uv run ruff check .
uv run ruff format --check .
```

Tests use local WebSockets, fake Discord objects, and/or a mock model. They do
not claim live Discord destination health, real-provider caching, production
Launchplane feedback resume, or full DUI parity. The complete remote-session
acceptance contract remains in [agent-session-protocol.md](agent-session-protocol.md).
