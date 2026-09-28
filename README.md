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
patch. Each Discord thread is named after the Codex thread's name or first
prompt. The bridge mirrors user messages, turn starts, completed turns with the
final answer, interrupts and errors. From Discord you can reply (stock starts a
turn or steers the running one), pause, ask for status, answer command approvals,
and answer `request_user_input` prompts. The bridge answers a Codex request only
after an explicit Discord decision. The first answer wins, so the TUI can still
answer. File-change, permission and other requests stay in the TUI. Continue,
new session and end session are not advertised.

Configure `~/.config/discord-blue/codex-bridge.toml`:

```toml
server_url = "wss://BRIDGE_HOST/agent-session/connect"
token_file = "~/.config/discord-blue/codex-bridge.token"  # mode 600; or set AGENT_SESSION_TOKEN
# socket_path = "~/.codex/app-server-control/app-server-control.sock"
# host_label = "Codex on Chris-Studio"
# idle_release_hours = 12
# allow_insecure_ws = false  # true permits ws:// to a trusted private host
```

Run it with `uv run discord-blue-codex-bridge`, or install
[the launchd template](docs/launchd/com.shinycomputers.discord-blue-codex-bridge.plist)
yourself. Behaviour and limits:

- It joins loaded root threads with `thread/resume` and sends no config
  overrides, because overrides can restart an idle thread cold. Joining does not
  clear a thread's goal. The owner receives a read-only goal snapshot:
  `thread/goal/updated`, or `thread/goal/cleared` when the thread has no goal.
- A joined thread stays loaded while the bridge is subscribed. After
  `idle_release_hours` without activity or pending prompts, the bridge
  unsubscribes so that a thread whose TUI has closed can unload. It rejoins when
  the thread becomes active again.
- Threads started with `--no-daemon`, `--profile` or most `-c` overrides run
  outside the daemon, so the bridge cannot see them.
- If the daemon connection drops, every session closes. When it reconnects, each
  session starts again with a new epoch, so old Discord controls are rejected.
- Run the stock end-to-end test with
  `CODEX_BIN=/path/to/codex uv run python -m unittest tests.test_codex_bridge_stock`.
  It uses a disposable app-server, synthetic auth and a fake model.

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
