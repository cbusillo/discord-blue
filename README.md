# Discord Blue LXC Installation Guide

Discord Blue is a basic Discord bot plugin system built with the **discord.py** library. This guide provides
step-by-step
instructions for setting it up on
Debian-based LXC.

## Installation Steps

1. Update System Packages:
   ```
   apt update && apt upgrade
   apt install git curl libcairo2
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Clone the Discord Blue Repository:
   ```
   cd /opt
   git clone https://github.com/cbusillo/discord-blue
   cd discord-blue
   ```

3. Install Dependencies with uv:
   ```
   uv venv
   ```

4. Set up and Start the Systemd Service:
   ```
   cp discord-blue.service /etc/systemd/system/
   system daemon-reload
   system enable discord-blue
   system start discord-blue
   ```

**Note**: Make sure to run once to create the config file and input the discord token along with the server and the bot
channel. Slash commands sync directly to the first guild the bot joins so they appear immediately.

```
uv run discord_blue
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
