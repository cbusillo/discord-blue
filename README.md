# Discord Blue

Discord Blue is a basic Discord bot plugin system built with the **discord.py**
library. It includes an agent-session bridge that connects Discord session
threads to a local remote agent inbox.

## LXC Installation

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

   For the existing root-based install, migrate the generated config instead:

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
sync directly to the first guild the bot joins, so they appear immediately.

To enable the agent-session bridge, set the doodad extension name in the
generated config:

```toml
[discord]
loaded_doodads = ["agent_session_doodad"]

[agent_session]
enabled = true
```

## Development

Install the managed Python environment:

```bash
uv sync --all-groups --python 3.13
```

Run the local validation gates:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy .
uv run python -m unittest discover -s tests -q
```

## Docker

A `Dockerfile` is provided to build a containerized version of the bot. It
uses the [`ghcr.io/astral-sh/uv:debian`](https://github.com/astral-sh/uv) base
image so `uv` is already available for dependency installation. The container
starts through a small entrypoint that aligns the non-root `discord-blue` user
with the mounted `/var/lib/discord-blue` owner, then runs the bot from that home
directory. That keeps the container compatible with the existing LXC/systemd
state directory during migration.

Build the image:

```bash
docker build -t discord-blue .
```

Run the bot with the existing service state mounted:

```bash
docker run --rm \
  --volume /var/lib/discord-blue:/var/lib/discord-blue \
  --publish 127.0.0.1:8787:8787 \
  discord-blue
```

Or use Docker Compose for a local smoke run:

```bash
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

When the agent-session doodad is loaded and `[agent_session].enabled` is `true`, the
same listener exposes an unauthenticated Launchplane-compatible health endpoint:

```bash
curl http://127.0.0.1:8787/health
```

`GET /health` returns JSON after the bot has reached Discord readiness and the
bridge listener has started. The payload includes the `discord-blue` service
name, package version, top-level status, Discord readiness component state, and
agent-session bridge state. The bridge reports heartbeat/reconciliation progress
and pending cleanup work. Closed WebSockets are excluded from the active-session
count. A failed or stalled monitor makes the enabled bridge unhealthy; Discord
readiness alone is not sufficient for a healthy response. The protected WebSocket route
is `/agent-session/connect` and requires the configured bearer token; `/health`
does not. The health component is `agent_session`.

See [the remote session contract](docs/agent-session-protocol.md) for client
integration. Discord Blue does not launch the agent or read local rollout files;
the client provides session events and optional reconnect history.

Launchplane may provide deployment identity through
`LAUNCHPLANE_RUNTIME_IDENTITY_JSON`. When set to a JSON object, Discord Blue
parses it and includes it as the `runtime_identity` object in the health
payload. Missing identity is omitted for local development. Malformed or
non-object identity values are represented as bounded `runtime_identity` error
objects so the response remains parseable and non-secret. Optional
`LAUNCHPLANE_SOURCE_GIT_REF` and `LAUNCHPLANE_IMAGE_REFERENCE` values are also
echoed when present.

## Codex bridge (runs on the Mac next to Codex)

`discord-blue-codex-bridge` attaches to the stock Codex app-server daemon as one
extra client and opens one agent session per live Codex thread. It needs no Codex
patch. Each Discord thread is named `🔷 <repo> · <label>`: the Codex thread's
name, else its first substantial prompt (short go-aheads such as "Continue"
don't count, as a name or a prompt), else a non-default git branch. Labels are
short, cut at a word boundary. Renaming the Codex thread renames the Discord
thread. The bridge mirrors user messages, turn starts, completed turns with the
final answer, interrupts and errors. From Discord you can reply (stock starts a
turn or steers the running one), pause, ask for status, answer command approvals,
and answer `request_user_input` prompts. A command approval shows Codex's
command exactly as the shell will run it; one Discord cannot show whole stays
in the TUI. The bridge answers a Codex request only
after an explicit Discord decision. The first answer wins, so the TUI can still
answer. File-change, permission and other requests stay in the TUI. Continue,
new session and end session are not advertised.

Configure `~/.config/discord-blue/codex-bridge.toml`:

```toml
server_url = "wss://BRIDGE_HOST/agent-session/connect"
token_file = "~/.config/discord-blue/codex-bridge.token"  # mode 600; or set AGENT_SESSION_TOKEN
# socket_path = "~/.codex/app-server-control/app-server-control.sock"  # overrides CODEX_HOME
# host_label = "Codex on Chris-Studio"
# allow_insecure_ws = false  # true permits ws:// to a trusted private host
# hello_timeout_seconds = 300  # how long to wait for Discord Blue to attach the thread
```

Run it with `uv run discord-blue-codex-bridge`, or install
[the launchd template](docs/launchd/com.shinycomputers.discord-blue-codex-bridge.plist)
yourself. Behaviour and limits:

- Every loaded root thread gets a Discord thread, which stays open for as long as
  the Codex thread stays loaded, even if it sits idle for days.
- The bridge subscribes to a thread (`thread/resume`, no config overrides, which
  can restart an idle thread cold) only while a turn runs, a prompt is pending,
  or a Discord reply arrives. It unsubscribes when the turn ends. Stock
  broadcasts status changes to every client, so the bridge rejoins when a turn
  starts, and stock replays pending approvals and questions on the join.
- When the TUI closes and no other client is subscribed, stock unloads the
  thread (after `thread_unload_delay_secs`, default 60) and reports `notLoaded`.
  The bridge then ends the Discord session and Discord Blue archives its thread
  with one "Session ended" line.
- Joining does not clear a thread's goal. Each join sends the owner a read-only
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
  resume the same thread in two daemons at once.

### Standalone TUI launches with Discord mirroring

A `codex` TUI process is visible only when its session lives in an app-server
that the bridge connects to. For example, `-c model_reasoning_effort=medium`
selects an embedded runtime on Codex 0.159.3 even when a daemon is running.
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
discord-blue-codex-bridge --codex-home "$CODEX_HOME"
# Multiple homes, using placeholders for account-specific directories:
# discord-blue-codex-bridge --codex-home ~/.codex --codex-home /path/to/account-home
```

These commands are future-launch setup, not a procedure to restart an active
bridge or convert running embedded sessions. Closing and resuming an existing
session on a daemon is an owner-coordinated action.

Run the stock end-to-end test with
`CODEX_BIN=/path/to/codex uv run python -m unittest tests.test_codex_bridge_stock`.
It uses disposable app-servers and TUIs, synthetic auth and a fake model,
including the embedded-versus-explicit-remote launch case.

## Claude Code sessions (the dui plugin)

This repository is also a local Claude Code plugin marketplace. Its `dui`
plugin gives each Claude Code CLI session its own Discord thread next to the
Codex sessions. Claude Code starts `discord-blue-claude-channel` once per
session as a [channel](https://code.claude.com/docs/en/channels-reference).
The thread is named `✳️ <repo> · <label>`: the session name (`-n` or
`/rename`), else the title Claude Code generates for the session (the one the
`/resume` picker shows, read from the end of the transcript), else the first
substantial typed prompt (tags such as pasted content and go-aheads such as
"continue" don't count), else a non-default git branch. Labels are short, cut at
a word boundary. It mirrors typed prompts and each turn's final answer. A Discord reply
becomes the session's next prompt. When Claude asks for a tool permission, the
thread says which tool is waiting; approve or deny it in the terminal. The thread archives when the session exits.

One-time setup on the Mac:

```sh
uv tool install --force /path/to/discord_blue-*.whl  # provides discord-blue-claude-channel
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
  reconnected, and says so. The thread gets a notice for each switch.
- Hooks reach the channel through an MCP tool, `dui_hook_event`, which the model
  can also see. Its description says never to call it, and calls that carry a
  model tool-use ID are ignored.
- Background sessions claimed from the Claude Code daemon do not show their
  launch flags, so they are assumed to have the channel.

## Launchplane/Dokploy migration target

Production deploys run through Launchplane and Dokploy. The product workflow
validates the repo, publishes an immutable GHCR image, and asks Launchplane to
deploy that image to the registered Dokploy application on the Discord Blue LXC.

- CI proves the Docker image builds for every PR and push.
- Production publishes both a digest and a `sha-<commit>` tag. The deploy request
  uses the digest as `artifact_id` and the tag as `deploy_reference`, so Launchplane
  can retain immutable evidence while Dokploy pulls the matching provider tag.
- The required `ci-gate` context fails closed unless both application
  validation and the container image build succeed.
- The production LXC keeps `/var/lib/discord-blue` as the durable state mount.
- Dokploy pulls a versioned image and replaces the container.
- Launchplane owns the deploy record, Dokploy mutation, and rollback decision.
- This repo keeps source, image build inputs, tests, and smoke-check guidance.

GitHub Actions expects repo-scoped self-hosted runners with these labels:

- `self-hosted`
- `chris-testing`
- `chris-testing-discord-blue`

This follows the product-repo pattern used by repos such as Sell Your Outboard
and VeriReel. Individual runners may also carry per-runner labels such as
`chris-testing-discord-blue-1` and `chris-testing-discord-blue-2` for targeted
maintenance.
