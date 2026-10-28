# Discord Blue

Discord Blue is a basic Discord bot plugin system built with the **discord.py**
library. It includes an agent-session bridge that connects Discord session
threads to local Codex app-server sessions and Claude Code channels.

Chris Busillo (the Director) uses Discord Blue for his own agent sessions and
is its Client.

## Standalone LXC/systemd installation

This is the standalone source-install path supported by `discord-blue.service`.
The repository's production workflow uses Launchplane/Dokploy containers instead;
see [production deployment](#launchplanedokploy-production-deployment). Do not run
both services against the same bot token or state directory.

1. Update System Packages:

   ```bash
   apt update && apt upgrade
   apt install git curl
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Create the service user:

   ```bash
   groupadd --system discord-blue
   useradd --system --home-dir /var/lib/discord-blue --create-home \
     --gid discord-blue --shell /usr/sbin/nologin discord-blue
   install -d -m 700 -o discord-blue -g discord-blue /var/lib/discord-blue/.config/discord-blue
   ```

3. Clone the Discord Blue Repository:

   ```bash
   cd /opt
   git clone https://github.com/cbusillo/discord-blue
   cd discord-blue
   ```

4. Install Dependencies with uv:

   ```bash
   export UV_PYTHON_INSTALL_DIR=/opt/discord-blue/.uv-python
   uv python install 3.13
   rm -rf .venv
   uv sync --all-groups --python 3.13
   chmod -R a+rX .uv-python .venv
   ```

5. Create or migrate the config.

   For a new install, run the bot once as the service user and complete the
   prompts:

   ```bash
   sudo -u discord-blue HOME=/var/lib/discord-blue \
     /opt/discord-blue/.venv/bin/discord-blue
   ```

   If migrating a root-based standalone install, copy its generated config instead:

   ```bash
   install -D -m 600 -o discord-blue -g discord-blue \
     /root/.config/discord-blue/config.toml \
     /var/lib/discord-blue/.config/discord-blue/config.toml
   ```

6. Set up and Start the Systemd Service:

   ```bash
   cp discord-blue.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable discord-blue
   systemctl start discord-blue
   ```

**Note**: The systemd service runs as the `discord-blue` user and reads config
from `/var/lib/discord-blue/.config/discord-blue/config.toml`. Slash commands
sync directly to the configured home guild, so they appear immediately. Foreign
guilds receive no session/setup commands; see [CM conversation](docs/cm-conversation.md).

To enable the agent-session bridge, set the doodad extension name in the
generated config:

```toml
[discord]
loaded_doodads = ["agent_session_doodad"]

[agent_session]
enabled = true
token = "REPLACE_WITH_A_SHARED_SECRET"
# channel_id = 123456789  # optional; defaults to discord.bot_channel_id
operator_role_name = "Agent Admin"  # existing Discord role whose members may use session controls
```

Use the same secret in the local client's token file. The WebSocket rejects an
empty server token. `operator_role_name` is the current config key for the
session admin permission; it falls back to `discord.employee_role_name`. If both
are empty, nobody can reply, approve, or use session controls, and the bridge
logs a warning when it starts.

## Development

The owner's [overall DIRECTION.md](https://github.com/cbusillo/direction/blob/HEAD/DIRECTION.md)
governs this repository; there is no separate repository DIRECTION.md.
[AGENTS.md](AGENTS.md) is the only agent-instruction file and points to the
maintained execution and review procedures. Repository workflow facts and
validation commands, including setup, lint, type check and tests, live in
`qualityGate` in [.github/github.json](.github/github.json).

## Docker

A `Dockerfile` is provided to build a containerized version of the bot. It
uses the [`ghcr.io/astral-sh/uv:debian`](https://github.com/astral-sh/uv) base
image so `uv` is already available for dependency installation. The container
starts through a small entrypoint that prepares the mounted `/var/lib/discord-blue`
directory for the `discord-blue` runtime user, then runs the bot from that home
directory. That keeps the container compatible with the existing LXC/systemd
state directory during migration.

Build the image:

```bash
docker build -t discord-blue .
```

For a standalone systemd-to-container migration, stop `discord-blue.service`
before running the bot with the existing service state mounted:

```bash
docker run --rm \
  --volume /var/lib/discord-blue:/var/lib/discord-blue \
  --publish 127.0.0.1:8787:8787 \
  discord-blue
```

For a new local Compose volume, first complete the bot's interactive token,
guild and channel setup, then stop the foreground bot before starting it detached:

```bash
docker compose run --rm discord-blue
# After setup, stop the foreground bot with Ctrl-C:
docker compose up -d
```

Compose creates a `discord-blue-state` volume mounted at
`/var/lib/discord-blue`. For the production LXC, Dokploy/Launchplane should bind
the real `/var/lib/discord-blue` directory instead so
`.config/discord-blue/config.toml` survives image replacement. Agent session
state remains on the client host.

The agent-session bridge listens on the configured `[agent_session]` host and port.
The image exposes port `8787`, and the local Compose file binds it to
`127.0.0.1:8787`.

### Managed server runtime inputs

An isolated server can start from an empty state volume using runtime inputs
delivered by Launchplane. `DISCORD_TOKEN` supplies `[discord].token` and
`AGENT_SESSION_TOKEN` supplies the server's `[agent_session].token`. Both are
optional overrides of local TOML, enabled only when `DISCORD_BLUE_RUNTIME_CONFIG`
is supplied; a supplied empty credential refuses startup in that mode.
The local bridge client separately supports `AGENT_SESSION_TOKEN`; supplying
the server's value does not configure the client.

`DISCORD_BLUE_RUNTIME_CONFIG` carries **non-secret TOML** settings in the
`discord` and `agent_session` sections. Fields use their existing config types;
unknown fields, other sections and `token` fields refuse startup. For example,
with sample IDs that must be replaced by the lane's recorded isolated IDs:

```toml
[discord]
guild_id = 123
bot_channel_id = 456
loaded_doodads = ["agent_session_doodad"]

[agent_session]
enabled = true
listen_host = "0.0.0.0"
listen_port = 8787
operator_role_name = "Agent Admin"
auto_join_user_ids = [789]
spawned_channel_id = 654
```

Managed runtime mode requires a Discord token and positive guild/channel IDs
from these inputs or existing local TOML, plus a bridge token when the bridge is
enabled. Incomplete input refuses startup before any interactive setup or save.
Runtime credential overrides trim surrounding whitespace, matching the client.

Store the non-secret TOML as a lane runtime setting and the two credentials as
that lane's managed secrets. The admin supplies secrets in Launchplane's
signed-in screens. Use a separate state volume and testing identity; do not
copy production state or credentials. Runtime overrides stay in memory and
are excluded from subsequent TOML saves, which preserve existing local values.
Without runtime inputs, local TOML and interactive setup retain their behavior.
The local development Compose file does not deliver these inputs.

`[agent_session].spawned_channel_id` is the separate channel for sessions the
Supervisor or a lead launches. Set it in the lane's existing
`DISCORD_BLUE_RUNTIME_CONFIG` through Launchplane's supported product-config
dry-run/apply path. Use that lane's existing Discord guild and an already
available channel; changing Discord access is a separate decision. Production
uses the setting only through a decided, gated release.

The launcher supplies `DISCORD_BLUE_SESSION_KIND=spawned`. Codex's daemon
ingress persists it as native `threadSource=spawned`; the Codex bridge sends
`session_kind=spawned` in hello. Claude's channel reads the inherited launcher
label and sends the same field. Missing labels, including ordinary TUI sessions
and the Director-opened Supervisor, use the main session channel and add
`auto_join_user_ids`. Spawned threads never add those users, including on
reopen. A new spawned session waits for a distinct configured second channel
instead of falling back to the main channel. Existing threads retain their
parent channel; recovery, replies, approvals and cleanup support both parents.
For question routing, follow the [Supervisor question queue](https://github.com/cbusillo/codex-skills/blob/main/skills/supervisor/SKILL.md).
The bridge mirrors the shared daemon's Codex sessions; embedded sessions retain
their existing visibility limit.

### Managed runtime Compose

[compose.runtime.yml](compose.runtime.yml) is the deployment entrypoint for an
isolated managed server. Select that file on the prepared provider target;
do not combine it with the local development Compose file. Launchplane selects
`LAUNCHPLANE_IMAGE_REFERENCE` as the verified `repository@sha256:digest` from the
canonical Build manifest, never a mutable tag or a local build. It also supplies
`LAUNCHPLANE_SOURCE_GIT_REF` and `LAUNCHPLANE_RUNTIME_IDENTITY_JSON` for that same
candidate and lane. Source checkout must be pinned to the reviewed completing
commit, independently of selecting the runtime image.

The Compose file explicitly passes those identity inputs, the non-secret TOML
and credentials described above, and `DISCORD_BLUE_STATE_DIR`. Every input must be nonempty before Compose can create a
container. Set `DISCORD_BLUE_STATE_DIR` to `/var/lib/discord-blue`, matching the
image's home and the Compose-scoped named volume; a different state-dir input
refuses at the image entrypoint. Keep the volume on the testing
target; no state, credentials or project name come from production. The image's
existing entrypoint initializes that volume and drops privileges before running
the server. Runtime credential overrides stay out of saved TOML; the provider's
managed environment remains sensitive and must not be dumped to logs or evidence.
The state-dir input is retained for compatibility with Launchplane's existing
managed-input contract; it confirms the image path rather than relocating state.
The provider must select a lane-unique Compose project name before creating
resources. Invalid managed inputs remain unhealthy under the restart policy
until corrected.

The lane's non-secret TOML must load `agent_session_doodad`, enable the bridge,
and listen on `0.0.0.0:8787`. The service name is `discord-blue`. Only the
target's default network reaches its HTTP `/health` and authenticated WebSocket
`/agent-session/connect` listener; this file publishes no host port. Launchplane
owns the private TLS overlay, certificates, receiver/monitor trust, activation
and recovery, as described in its
[private transport contract](https://github.com/cbusillo/launchplane/blob/main/docs/private-testing-transport.md).
It must qualify the provider's actual converted source, network, volume and
image bindings before activation. Compose validation is not live TLS, healthy
Discord login or deployment proof. Missing credentials or invalid runtime TOML
refuse startup; `/health` becomes available only after Discord readiness.

For updates, use Launchplane's reviewed artifact/source selection and gated
testing deployment, preserving this target's named state. Recover an uncertain
operation with its original key and supported readback. Roll back through the
same service path to the previous verified immutable artifact/source; preserve
the state volume. Listener-only recovery belongs to Launchplane and must preserve
this baseline. Do not use `down --volumes` on a retained lane.

The opt-in container qualification uses a disposable local image and project,
fictional credentials, the actual image entrypoint, and a stubbed Discord login:

```sh
docker build --tag discord-blue-runtime-fixture .
DISCORD_BLUE_COMPOSE_TEST_IMAGE="$(docker image inspect --format '{{.Id}}' discord-blue-runtime-fixture)" \
  uv run python -m unittest tests.test_runtime_compose -v
docker image rm discord-blue-runtime-fixture
```

It checks input refusal, in-memory delivery, non-root startup and non-persistence
without contacting Discord, reading a real token or installing a local client.
Its temporary project/state are removed afterward. Ordinary unit validation skips
this explicit artifact fixture; Build's image job runs it against the loaded
image before the required gate can pass. Publishing also pulls and qualifies the
exact pushed digest before producing the artifact manifest used by Launchplane.
Registry tags can move before that post-push check finishes; only the digest in
a successful canonical Build manifest qualifies for deployment.

When the agent-session doodad is loaded and `[agent_session].enabled` is `true`, the
same listener exposes an unauthenticated Launchplane-compatible health endpoint:

```bash
curl http://127.0.0.1:8787/health
```

`GET /health` returns JSON after the bot has reached Discord readiness and the
bridge listener has started. The payload includes the `discord-blue` service
name, package version, top-level status, Discord readiness component state, and
agent-session bridge state. The bridge reports heartbeat/reconciliation progress
and pending cleanup work, plus session-store health and its record count. Closed WebSockets are excluded from the active-session
count. A failed or stalled monitor makes the enabled bridge unhealthy; Discord
readiness alone is not sufficient for a healthy response. The protected WebSocket route
is `/agent-session/connect` and requires the configured bearer token; `/health`
does not. The health component is `agent_session`.

See [the remote session contract](docs/agent-session-protocol.md) for client
integration. Discord Blue does not launch the agent or read local rollout files;
the client provides session events and optional reconnect history.

Discord health also requires a recent heartbeat ACK processed on the current
gateway connection. Its freshness budget is twice the heartbeat interval supplied
by Discord's HELLO: one interval for the next heartbeat and one for its ACK.
Missing or expired ACK proof returns HTTP 503 even if the process is running.
Reconnection clears old proof until the new connection receives an ACK and a
READY or RESUMED event. Quiet
channels need no messages to stay healthy. The response exposes only cadence,
ACK age and a bounded reason, never gateway payloads or credentials. This uses
Discord's [gateway heartbeat protocol](https://github.com/discord/discord-api-docs/blob/main/developers/events/gateway.mdx).

Launchplane may provide deployment identity through
`LAUNCHPLANE_RUNTIME_IDENTITY_JSON`. When set to a JSON object, Discord Blue
parses it and includes it as the `runtime_identity` object in the health
payload. Missing identity is omitted for local development. Malformed or
non-object identity values are represented as bounded `runtime_identity` error
objects so the response remains parseable and non-secret. Optional
`LAUNCHPLANE_SOURCE_GIT_REF` and `LAUNCHPLANE_IMAGE_REFERENCE` values are also
echoed when present.

## Session answers and dashboard

Both harnesses render final answers in a compact Components V2 card. Long
answers show a code-fence-balanced preview and an **answer.md** file with the
complete original Markdown. If Discord denies the upload, the answer uses the
existing bounded text delivery path, with a terminal notice past its cap.
Short answers need no attachment. Answers still
notify normally and cannot ping people or roles. Restart recovery recognizes
both new answer cards and older text answers, so it does not repost them.

`/code active` shows operators a private dashboard snapshot, with sessions
waiting on you first, then uncertain or failed sessions, working sessions and
completed sessions. Each attached session has an **Open thread** button.
Disconnected sessions are excluded. Status and Harness menus filter both Codex
and Claude sessions. **Waiting on you** includes unconfirmed approvals that
need a terminal check. **Previous**, **Next**, and **Refresh** update the same
private message from the live session list. Changing a filter returns to page
one; Refresh preserves the current page when it still exists. The snapshot
time shows when the list was read. Controls expire after ten minutes idle;
run `/code active` again to open a new dashboard.

[Phone layout preview](docs/assets/dui/answers-dashboard-preview.html) uses
serialized component payloads. Discord controls final typography and sizing.

## Codex bridge (runs on the Mac next to Codex)

`discord-blue-codex-bridge` attaches to the stock Codex app-server daemon as one
extra client and opens one agent session per live Codex thread. It needs no Codex
patch. Each Discord thread is named `🔷 <repo> · <label>`: the Codex thread's
name, else its first substantial prompt (short go-aheads such as "Continue"
don't count, as a name or a prompt), else a non-default git branch. Labels are
short, cut at a word boundary. Renaming the Codex thread renames the Discord
thread. The bridge mirrors user messages, turn starts, completed turns with the
final answer, interrupts and errors. From Discord you can reply (stock starts a
turn or steers the running one), pause, ask for status, answer command, file-change and permission approvals,
and answer `request_user_input` prompts. A command approval shows Codex's
command exactly as the shell will run it; one Discord cannot show whole stays
in the TUI. The bridge answers a Codex request only
after an explicit Discord decision. Approval cards offer **Approve once** and
**Deny** buttons beneath the full request; reaction controls remain supported.
The card removes its controls when a decision is sent, then shows whether the
local agent acknowledged it, the request expired, or the native terminal resolved it. The first answer wins, so the TUI can still
answer. File-change approvals show the full proposed changes, including paths,
change kinds and diffs; permissions show the full request and grant only for
the current turn. Oversized or fenced content, unknown fields, unavailable
patches and session-wide file grants stay in the TUI. A changed patch retires
its Discord prompt. MCP elicitation and other requests stay in the TUI.
Continue and end session are not advertised.

To choose a repository for a new session, use the existing Codex Discord thread
for that repository, then run /code new there. Stock starts a fresh thread in
that exact folder with its configured trust and permissions; the bridge mirrors it, and its first substantive prompt supplies its title. Reply in the new Discord thread to start work. The bridge keeps
its own new threads subscribed while running, so they stay available without
an open TUI. Daemon reconnects rediscover them if they remain loaded.

Configure `~/.config/discord-blue/codex-bridge.toml`:

```toml
server_url = "wss://BRIDGE_HOST/agent-session/connect"
token_file = "~/.config/discord-blue/codex-bridge.token"  # mode 600; or set AGENT_SESSION_TOKEN
# socket_path = "~/.codex/app-server-control/app-server-control.sock"  # overrides CODEX_HOME
# host_label = "Codex on Chris-Studio"
# allow_insecure_ws = false  # true permits ws:// to a trusted private host
# hello_timeout_seconds = 300  # how long to wait for Discord Blue to attach the thread
```

Keep `hello_timeout_seconds` above the server's 15-second pending-reply interval
(default 300 seconds) so a thread-creation cooldown keeps the connection waiting.

Run it with `uv run discord-blue-codex-bridge`, or install
[the launchd template](docs/launchd/com.shinycomputers.discord-blue-codex-bridge.plist)
yourself. Behaviour and limits:

- Every loaded root thread gets a Discord thread, which stays open for as long as
  the Codex thread stays loaded, even if it sits idle for days.
- A thread is mirrored once it has a name or a first prompt. A prompt passed on
  the command line (`codex … -- "Your task"`) starts the turn a few seconds
  before stock records it, so the bridge reads a busy, unnamed thread again for
  about 15 seconds, and also joins when a thread is named. The bridge log
  records every thread it does not join, with the reason.
- For TUI-owned threads, the bridge subscribes to a thread (`thread/resume`, no config overrides, which
  can restart an idle thread cold) only while a turn runs, a prompt is pending,
  or a Discord reply arrives. It unsubscribes when the turn ends. Stock
  broadcasts status changes to every client, so the bridge rejoins when a turn
  starts, and stock replays pending approvals and questions on the join.
- When the TUI closes and no other client is subscribed, stock unloads the
  thread (after `thread_unload_delay_secs`, default 60) and reports `notLoaded`.
  The bridge then ends the Discord session and Discord Blue archives its thread
  with one "Session ended" line.
- Joining does not clear a thread's goal. Each join sends the TUI client a read-only
  goal snapshot: `thread/goal/updated`, or `thread/goal/cleared` when the thread
  has no goal.
- Threads started with `--no-daemon`, `--profile` or most `-c` overrides run
  outside the daemon, so the bridge cannot see them.
- If the daemon connection drops, every session disconnects. Discord Blue keeps
  their threads open for five minutes; a session that reconnects within that time
  continues in the same thread, with a new epoch, so old Discord controls are
  rejected.
- Without an explicit `socket_path`, the bridge follows `CODEX_HOME` (default
  `~/.codex`). `CODEX_SQLITE_HOME` shares saved history, not the live app-server
  transport or the account used by the server.
- One bridge process can mirror multiple account-home daemons. Pass
  `--codex-home` once per home; these options override the configured socket.
  Each connection reconnects independently. Point only one bridge at each
  daemon. Keep the homes separate when they use different accounts, and do not
  resume the same thread in two daemons at once. The configured host label stays
  unchanged across home selection or ordering changes, so the server can find
  existing Discord threads after a restart. Account-home names are not exposed.
  An unexpected bridge failure retries with increasing delay (up to one minute),
  while the other homes keep mirroring.

### Standalone TUI launches with Discord mirroring

A `codex` TUI process is visible only when its session lives in an app-server
that the bridge connects to. Codex 0.159.3's
[daemon eligibility policy](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/tui/src/daemon_startup.rs)
excludes overrides such as `-c model_reasoning_effort=medium` from implicit
daemon attachment, selecting an embedded runtime even when a daemon is running.
Those embedded sessions were never observable through this daemon bridge.
The bridge cannot attach to them retroactively or infer live control from
shared SQLite history.

For the **next launch**, use Codex's built-in explicit endpoint selection.
In a terminal with the intended `CODEX_HOME` already selected, start that
home's daemon if needed, then connect the TUI to it:

```sh
codex app-server daemon start
codex --remote unix:// -m gpt-6.1-sol -c model_reasoning_effort=medium -- "Your task"
```

`unix://` selects the endpoint for that `CODEX_HOME`. The server uses its own
home's credentials and configuration; do not point an account-specific client
at another account's server. Arbitrary server configuration overrides and
profiles must be configured on the intended server, rather than assuming the
remote client changes them. Model and reasoning selection are covered by the
native TUI stock test.

At the next bridge launch, select the same home (or repeat for several homes):

```sh
discord-blue-codex-bridge --codex-home "${CODEX_HOME:-$HOME/.codex}"
# Multiple homes, using placeholders for account-specific directories:
# discord-blue-codex-bridge --codex-home ~/.codex --codex-home /path/to/account-home
```

For Supervisor accounts, use the same private account configuration as its
launcher instead of copying home paths into the login item:

```sh
discord-blue-codex-bridge --supervisor-config ~/.code/skill-data/supervisor.toml
```

The bridge reads every `provider = "openai"` entry's `env.CODEX_HOME` from
`[[accounts.account]]`, including accounts currently out of capacity. It does
not run the capacity reader or open credential files. Changes to this list
take effect on the next bridge restart. `--supervisor-config` and
`--codex-home` are alternative selections; invalid or empty OpenAI mappings
fail startup rather than silently selecting a default home.
Supervisor homes are added alongside the bridge's configured/default socket,
preserving existing standalone sessions. Duplicate sockets connect only once.
Explicit `--codex-home` selections continue to override the configured socket.

For each selected home, the bridge runs `codex app-server daemon start` once
before connecting. Native start reuses an already running daemon; it does not
bootstrap or restart it. Startup failures identify the home and exit code or
error type without echoing provider output, and keep that home's reconnect
loop running independently of the others. A start command is bounded to 60
seconds. Later daemon loss uses the existing socket reconnect behavior; the
bridge does not repeatedly start daemons. Without a home selection, the
configured/default socket is preserved: standard account sockets identify a
home for native start, while custom socket paths only reconnect.

New daemons inherit the bridge's tool PATH, so set launchd's
`EnvironmentVariables.PATH` to include your installed MCP/tool executables;
the template includes common macOS locations. The bridge removes its session
token and inherited `CODEX_*`, `OPENAI_*`, and `AZURE_OPENAI_*` overrides,
then selects only the target `CODEX_HOME`. Each daemon uses that home's
credentials/configuration. For intentional provider environment overrides,
start the daemon separately with that home's intended environment before
starting the bridge; native start reuses the running daemon unchanged.
The bridge also removes `VIRTUAL_ENV` and its tool directory from PATH and
starts the command from the stable user home, so a daemon does not inherit
the bridge's Python environment or hold a task worktree open.
Other environment variables come from the bridge's launcher for the daemon's
lifetime. Put any tool or MCP environment it needs in the login item's
`EnvironmentVariables`; launchd does not load your terminal's shell profile.

These commands are future-launch setup, not a procedure to restart an active
bridge or convert running embedded sessions. Closing and resuming an existing
session on a daemon needs coordination with the Director (the person whose
direction the agents follow).
Use [routine bridge delivery](#routine-bridge-delivery) to deliver merged client
changes to both launch paths. `uv run discord-blue-codex-bridge` uses the checkout
version for development.
If launchd owns the bridge, put `--supervisor-config` and the absolute path of
the launcher's private config in its existing `ProgramArguments`, then run the
routine delivery command to reload that definition. Set `--codex-bin` to the
absolute installed Codex executable
because launchd has a minimal PATH; replace `CODEX_BIN` in the template. For
standalone use, repeat `--codex-home` instead. Do not
start an additional manual process against the same daemon.

Run the stock end-to-end test with
`CODEX_BIN=/path/to/codex uv run python -m unittest tests.test_codex_bridge_stock`.
It uses disposable app-servers and TUIs, synthetic auth and a fake model,
including the embedded-versus-explicit-remote launch case. The stock fixture
uses a foreground `app-server --listen unix://` listener; it does not test
installing or managing a daemon service.

## Claude Code sessions (the dui plugin)

This repository is also a local Claude Code plugin marketplace. Its `dui`
plugin gives each Claude Code CLI session its own Discord thread next to the
Codex sessions. Claude Code starts `discord-blue-claude-channel` once per
session as a [channel](https://code.claude.com/docs/en/channels-reference).
The thread is named `✴️ <repo> · <label>`: the session name (`-n` or
`/rename`), else the title Claude Code generates for the session (the one the
`/resume` picker shows, read from the end of the transcript), else the first
substantial typed prompt (tags such as pasted content and go-aheads such as
"continue" don't count), else a non-default git branch. Labels are short, cut at
a word boundary. It mirrors typed prompts and each turn's final answer. A Discord reply
becomes the session's next prompt. When Claude asks for a tool permission, the
thread says which tool is waiting; approve or deny it in the terminal. The thread archives when the session exits.

One-time setup on the Mac:

```sh
uv build --wheel --clear  # initial installation only
uv tool install --force dist/discord_blue-*.whl
claude plugin marketplace add ~/Developer/discord-blue
claude plugin install dui@discord-blue
# In ~/.zshrc, add the channel flag to the existing alias:
alias claude='claude --allow-dangerously-skip-permissions --dangerously-load-development-channels plugin:dui@discord-blue'
```

The channel reads the Codex bridge's `~/.config/discord-blue/codex-bridge.toml`
(`server_url`, `token_file`, `allow_insecure_ws`, `hello_timeout_seconds`); its host label is always
`Claude Code on <host>`. Each launch shows Claude Code's "Loading development
channels" warning; press Enter. A session that is already running cannot load
the channel: `/exit`, then `claude --resume <session id>` through the alias. It
keeps the same session ID, name and transcript.

Behaviour and limits:

- Custom channels are a Claude Code research preview. Only the
  `--dangerously-load-development-channels` flag enables one, the flag's
  warning appears on every launch, and the flag and protocol may change.
- The plugin loads in every session. A session started without the flag is
  still mirrored, but Claude Code drops channel messages, so the thread offers
  no replies and says how to resume with the flag.
- Discord cannot pause, interrupt or end a Claude Code turn, and cannot answer
  Claude's multiple-choice questions. A reply sent while Claude is working is
  held, because Claude Code would otherwise queue it and could deliver it after
  `/clear` or `/resume`. Held replies go out only when Claude Code reports it is
  idle: its `idle_prompt` notification, about a minute after Claude finishes and
  only while nobody is typing in the terminal. Each such moment delivers and
  acknowledges the oldest held reply; the rest wait for the next. Held replies
  are dropped, with a notice, if the conversation switches first.
- Discord cannot approve or deny Claude Code's permission prompts. Claude
  Code's channel permission request carries only a display preview and no
  tool-call ID, so a Discord decision could not be tied reliably to the call
  it would allow. The notice names only the tool: the preview can hold secrets
  Claude Code does not mask, such as passwords or private keys.
- `/clear` or `/resume` inside a session keeps its thread but reconnects under a
  new epoch, so replies sent for the previous conversation are rejected. Discord Blue also refuses a reply written before the session last
  reconnected, and says so. A reply received while the session is still attaching
  gets a reconnecting notice asking you to send it again; it is not delivered
  ahead of the connection acknowledgment. The thread gets a notice for each switch.
- Hooks reach the channel through an MCP tool, `dui_hook_event`, which the model
  can also see. Its description says never to call it, and calls that carry a
  model tool-use ID are ignored.
- Background sessions claimed from the Claude Code daemon do not show their
  launch flags, so they are assumed to have the channel.

## Replying with context and answering together

Long-press one of the bot's answer messages in a session thread and choose
**Apps → Reply about this**. The form shows the session, a bounded excerpt of
that message, and its jump link. Your submitted reply includes that context in
the thread and goes through ordinary reply delivery. Earlier answers are marked
as earlier context. Reconnecting, deleting or changing the source answer, or
losing operator access invalidates the open form. It expires after ten minutes.
For a long answer, choose the card containing the passage you mean. The form
reserves space for the context in Discord's message limit; use an ordinary
thread reply for longer text.

A small questionnaire offers **Answer together**, with one radio group per
choice question and an optional text field only where Other is allowed. Choose
Other to send that written answer. Submit once to send all answers. Closing the
form leaves the request waiting. The existing pickers and **Send answer** stay
available, including after the form expires. Long questions or option meanings,
secret questions, too many fields, and choices beyond Discord's radio limits
use the picker instead of a shortened form. These forms require discord.py 2.7.

Reply about this works with Codex and enabled Claude channels; Claude still
holds busy replies until idle. Answer together is for Codex structured questions.
It does not add Claude native question responses or permission decisions.
Discord controls the final appearance on a phone.

## Routine bridge delivery

The release agent runs one local command from a clean, committed checkout of
merged `main`, after fetching and verifying the selected release revision:

```sh
uv run python scripts/deliver_bridges.py \
  --checkout /path/to/discord-blue \
  --build-dir /Volumes/Developer-Artifacts/task-evidence/discord-blue/bridge-delivery/build
```

This is the supported delivery step for **both** local clients in every DUI
release. Server deployment alone does not deliver the bridges. Chris does not
build/install a wheel or restart a bridge. This command does not release or
configure the Discord server; those operations retain their Launchplane gates.

The command fetches `origin/main` and refuses uncommitted changes or a HEAD
outside its history. It snapshots committed files and installs using the lockfile
into `~/.local/share/discord-blue-bridges/<revision>/venv`. Neither running
runtimes nor token/config files are modified. Rollback uses the same command
from a clean checkout at the previous release revision.

Both `~/.local/bin/discord-blue-*` launchers select that runtime. The existing
DUI plugin uses the Claude launcher through PATH, so the next ordinary session
gets the release; active Claude sessions finish on their previous runtime.
The older `uv tool` install is superseded. Retain it while old Claude sessions
use it, and do not run `uv tool install`, `upgrade` or `uninstall discord-blue`:
those commands can replace these launchers. Use this delivery command instead.

For Codex, the command points the existing login item at the stable launcher,
preserving its other arguments and environment. Every delivery unloads that
job with a bounded `launchctl bootout --wait`, then bootstraps its disk definition.
This also applies argument/environment edits and recovers interrupted updates. Open Discord controls
expire and sessions reconnect with new epochs during the brief interruption.
Codex's native daemons run in separate process groups; their TUIs keep running.
The existing login item must be recognized as
`com.shinycomputers.discord-blue-codex-bridge` in the signed-in user's GUI domain.
An unloaded recognized job is bootstrapped automatically, including recovery
from interrupted delivery. A reload failure reports incomplete delivery.

The command prints the selected revision and a reload receipt. That is not
proof that startup completed. The release agent checks the new job PID and its `Bridge source <version> <revision>` startup log line,
and verifies the next Claude launch's startup line. Both installed commands
also support a credential-free revision check:

```sh
~/.local/bin/discord-blue-codex-bridge --version
~/.local/bin/discord-blue-claude-channel --version
```

Retain previous runtimes until no active session uses them. Repeating delivery
replaces the Codex bridge again; it does not restart active Claude sessions.

## Launchplane/Dokploy production deployment

Launchplane's gated path and Dokploy are the only production release route. The
product workflow validates the repo and publishes an immutable GHCR image; it
does not request production deployment. Testing and release qualification remain
tracked in [launchplane#2792](https://github.com/cbusillo/launchplane/issues/2792).

A push to `main`, including a PR merge, runs the canonical
[Build workflow](.github/workflows/build.yml). It validates, builds and publishes
the image, then uploads `artifact-manifest-<run_attempt>` binding the source
commit to the immutable registry digest. Launchplane reads that successful push
run under its [artifact provenance contract](https://github.com/cbusillo/launchplane/blob/main/docs/artifact-provenance.md).
Pull requests and `launchplane/train/**` pushes validate and build without
publishing. Manual Build runs on `main` publish, but are not release provenance.

Main pushes feed Launchplane's testing reconciliation only; they do not request
production deployment. The [publication reader](.github/workflows/main.yml)
retains its bounded read of the successful canonical Build and current-attempt
manifest for the exact commit, without calling Launchplane.
When retrying a main Build, use **Re-run all jobs** so the latest run attempt
uploads its own manifest. Re-running only failed jobs can leave the latest
attempt without the manifest required by Launchplane. Rebuilding republishes
the SHA tag; release qualification must use the latest attempt's digest.
The read-only publication wait uses a GitHub-hosted runner so it cannot occupy
the self-hosted runners needed by Build. The required `ci-gate` proves validation
and image build; it does not prove a production release.
Consult the [overall direction](https://github.com/cbusillo/direction/blob/HEAD/DIRECTION.md)
for release authority before production work.

- CI proves the Docker image builds for every PR and push with read-only permissions.
- A separate publishing job runs only on `main` pushes and manual `main` runs;
  it alone has GHCR package-write permission. Pull-request jobs are read-only.
- Build publishes both a digest and a commit tag from
  [the shared image metadata](scripts/image_metadata.py); its artifact manifest
  records the immutable digest for Launchplane.
- The required `ci-gate` context fails closed unless both application
  validation and the container image build succeed, plus publishing on runs that
  publish an image, including its manifest upload.
- The production LXC keeps `/var/lib/discord-blue` as the durable state mount.
- The bridge saves session recovery hints in `$HOME/agent-sessions.json`
  (`/var/lib/discord-blue/agent-sessions.json` in the container). One writer
  atomically replaces the file. Shutdown closes sockets and keeps live threads
  and their notifications open. On startup, stored sessions receive at least
  five minutes to reconnect; a confirmed session end still archives immediately.
  Missing or corrupt state falls back to Discord discovery. See the
  [restart behavior](docs/agent-session-protocol.md#disconnect-cleanup-and-recovery) for recovery limits.
- Dokploy pulls a versioned image and replaces the container.
- Launchplane owns the deploy record, Dokploy mutation, and rollback decision.
- This repo keeps source, image build inputs, tests, and smoke-check guidance.

GitHub Actions runs on repo-scoped self-hosted runners; the `runs-on` labels in
[the build workflow](.github/workflows/build.yml) are the source of truth.

## Icon artwork

The retained [charcoal-and-orange DUI icon](docs/assets/dui-icon.png) is original
artwork: paired conversation bubbles and terminal chevrons. It is designed for
Discord's circular avatar crop and checked at 32, 40 and 128 pixels.
It is not applied to the bot or server. Session thread names use standard emoji:
Claude uses the orange eight-pointed star (✴️), and Codex uses the blue diamond (🔷).

## Session status cards

New threads keep machine identity in a **Session details** card hidden by
Discord's native tap-to-reveal control. The complete marker stays in Discord
history, so details and recovery survive a lost local store. Older text markers
remain recognized. `/code status` also shows current or stored details privately
to operators. Spoilers are a reading aid, not access control: details keep the
thread's existing audience. Routine parent connection announcements are no longer
posted. Use `/code active` and **Open thread** to find sessions you can access.
Configured users already in a thread are not added again; completed session
cleanup still removes members, archives and locks the thread.

A new thread briefly settles its initial name before renaming, so a title learned
just after connection can replace the temporary name in one edit. Later changes
to capitalization, spacing or trailing sentence punctuation do not rename it,
including any fallback branch label.
The information reaction refreshes and acknowledges on the live card without a
public status echo. Questions, approvals and failures still get new messages.

Codex and Claude session threads use one persistent Components V2 status card:
**Working**, **Waiting on you**, **Turn finished**, **Failed**, or **Disconnected**.
A finished turn or disconnected session does not claim that a task succeeded.
The card includes the session label, latest status detail, reaction controls and
a relative update time. Ordinary terminal input and turn completion edit it in
place. Its message ID is saved with session recovery hints, so reconnect can
find it even after a long conversation. Legacy recovery searches thread history;
a read or edit failure keeps the anchor, and confirmed deletion permits a replacement.
Status-card edits suppress mentions; initial cards suppress notifications too.
Questions, approvals, terminal-action requests and errors remain new messages.
Routine session closure updates the card and keeps the existing archive/reopen
lifecycle, without another session-ended post.

A spawned session omits an automated launch brief only when its client marks
that exact input with native launch provenance. The card says where to read the full input in the native
terminal. Later human terminal input remains visible. Existing, resumed or uncertain inputs keep their mirrors. Codex proves the
first turn from native pagination. Claude matches the explicit CLI launch brief against the first hook input, with a
new transcript not yet created by Claude, or a fully read transcript containing no prior prompt.
Resumed launches, disabled session persistence and unreadable or long transcripts keep
the input visible. This display choice
does not change input delivery to either agent.

Assistant answers remain separate so thread-history recovery can
recognize them after a restart. Long answers have one Answer heading and continue inline across ordered
cards, with code fences reopened on each card. Ordinary long answers
stay inline. Huge output uses an `answer.md` attachment and shows all
bold Q-number question sections inline, with nearby recommendation and option blocks within a bounded excerpt per question.
Every later question still appears. Oversized context continues in the full answer. If there are no numbered
questions, the first chunk stays inline. If attachments are unavailable, the
inline content remains readable and the complete answer stays in the terminal.
Only the first card notifies. If delivery stops between cards, reconnect backfill
reposts the answer so its tail is not lost; the delivered prefix may repeat.

[Local phone layout preview](docs/assets/dui/status-390.png) renders serialized
component payloads; final typography and spacing are controlled by Discord.
The update time makes stale cards visible. On a reconnect, the bridge recovers
its existing status card and shows that state is unconfirmed until the next native update. Reaction controls remain
bound to the current connection and epoch. Paused turns return to a waiting card,
and Codex decision completion returns the card to working. Claude permission
previews have no tool-call identifier, so subsequent tool activity shows
**Check terminal** until completion confirms the outcome; parallel tools cannot
claim that an unrelated approval was resolved.
