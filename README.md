# Discord Blue LXC Installation Guide

Discord Blue is a basic Discord bot plugin system built with the **nextcord** library. This guide provides step-by-step
instructions for setting it up on
Debian-based LXC.

## Prerequisites

Have `git`, `curl`, `libcairo2`, and `python3.12` installed.

```
apt update
apt install git curl python3.12 libcairo2
```

## Installation Steps

1. Update System Packages:
   ```
   apt update
   ```

2. Clone the Discord Blue Repository:
   ```
   cd /opt
   git clone https://github.com/cbusillo/discord-blue
   cd discord-blue
   ```

3. Install Dependencies with uv:
   ```
   uv venv --dev
   ```

4. Set up and Start the Systemd Service:
   ```
   cp discord-blue.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable discord-blue
   systemctl start discord-blue
   ```
   The service now runs using `/opt/discord-blue/.venv/bin/python` to match the
   Python version created by `uv venv`.

**Note**: Make sure to run once to create the config file and input the discord token along with the server and the bot
channel.

```
uv run python -m discord_blue
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
