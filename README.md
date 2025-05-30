# Discord Blue LXC Installation Guide

Discord Blue is a new basic discord bot plugin system. This guide provides step-by-step instructions for setting it up on
Debian-based LXC.

## Prerequisites

Ensure you have `git`, `curl`, `libcairo2`, and `python3` installed.

```
apt update
apt install git curl python3.11 libcairo2
```

## Installation Steps

1. Update System Packages:
   ```
   apt update
   ```

2. Install Python Poetry:
   ```
   curl -sSL https://install.python-poetry.org | python3.11 -
   ```

3. Clone the Discord Blue Repository:
   ```
   cd /opt
   git clone https://github.com/cbusillo/discord-blue
   cd discord-blue
   ```

4. Install Dependencies using Poetry:
   ```
   /root/.local/bin/poetry install
   ```

5. Setup and Start the Systemd Service:
   ```
   cp discord-blue.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable discord-blue
   systemctl start discord-blue
   ```

**Note**: Make sure to run once to create the config file and input your discord token along with server and bot channel.

```
/root/.local/bin/poetry run discord-blue
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

The container entrypoint uses `uv run` so any project dependencies are
isolated and executed with the version pinned in `pyproject.toml`.

