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
   if [ ! -e /var/lib/discord-blue/.code ] && [ -e /root/.code ]; then
     cp -a /root/.code /var/lib/discord-blue/.code
     chown -R discord-blue:discord-blue /var/lib/discord-blue/.code
   fi
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
generated config. The compatibility config names remain `every_code` until the
Codex Lab migration is complete:

```toml
[discord]
loaded_doodads = ["every_code_doodad"]

[every_code]
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
`.config/discord-blue/config.toml` and `.code` agent state survive image
replacement.

The agent-session bridge listens on the configured `[every_code]` host and port.
The image exposes port `8787`, and the local Compose file binds it to
`127.0.0.1:8787`.

When the agent-session doodad is loaded and `[every_code].enabled` is `true`, the
same listener exposes an unauthenticated Launchplane-compatible health endpoint:

```bash
curl http://127.0.0.1:8787/health
```

`GET /health` returns JSON after the bot has reached Discord readiness and the
bridge listener has started. The payload includes the `discord-blue` service
name, package version, top-level status, Discord readiness component state, and
informational agent-session bridge state. The preferred protected WebSocket route
is `/agent-session/connect`; the legacy `/every-code/connect` route remains
available during migration. Both WebSocket routes require the configured bearer
token; `/health` does not.

Launchplane may provide deployment identity through
`LAUNCHPLANE_RUNTIME_IDENTITY_JSON`. When set to a JSON object, Discord Blue
parses it and includes it as the `runtime_identity` object in the health
payload. Missing identity is omitted for local development. Malformed or
non-object identity values are represented as bounded `runtime_identity` error
objects so the response remains parseable and non-secret. Optional
`LAUNCHPLANE_SOURCE_GIT_REF` and `LAUNCHPLANE_IMAGE_REFERENCE` values are also
echoed when present.

## Launchplane/Dokploy migration target

Production deploys run through Launchplane and Dokploy. The product workflow
validates the repo, publishes an immutable GHCR image, and asks Launchplane to
deploy that image to the registered Dokploy application on the Discord Blue LXC.

- CI proves the Docker image builds for every PR and push.
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
