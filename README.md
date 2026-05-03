# Discord Blue

Discord Blue is a basic Discord bot plugin system built with the **discord.py**
library. It includes an Every Code doodad that bridges Discord session threads
to a local Every Code remote inbox.

## LXC Installation

1. Update System Packages:

   ```bash
   apt update && apt upgrade
   apt install git curl
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Clone the Discord Blue Repository:

   ```bash
   cd /opt
   git clone https://github.com/cbusillo/discord-blue
   cd discord-blue
   ```

3. Install Dependencies with uv:

   ```bash
   uv sync --all-groups --python 3.13
   ```

4. Set up and Start the Systemd Service:

   ```bash
   cp discord-blue.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable discord-blue
   systemctl start discord-blue
   ```

**Note**: Make sure to run once to create the config file and input the Discord
token along with the server and the bot channel. Slash commands sync directly to
the first guild the bot joins, so they appear immediately.

```bash
uv run discord-blue
```

To enable Every Code, set the doodad extension name in the generated config:

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
image so `uv` is already available for dependency installation.

Build the image:

```bash
docker build -t discord-blue .
```

Run the bot:

```bash
docker run --rm discord-blue
```

Or use Docker Compose, which mounts `${HOME}/.config/discord-blue` into the
container for the generated config:

```bash
docker compose up -d
```
