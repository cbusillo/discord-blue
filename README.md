# Discord Blue LXC Installation Guide

Discord Blue is a basic Discord bot plugin system built with the **nextcord** library. This guide provides step-by-step instructions for setting it up on
Debian-based LXC.

## Prerequisites

Ensure you have `git`, `curl`, `libcairo2`, and `python3.12` installed.

```
apt update
apt install git curl python3.12 libcairo2
```

## Installation Steps

1. Update System Packages:
   ```
   apt update
   ```

2. Install Python Poetry:
   ```
   curl -sSL https://install.python-poetry.org | python3.12 -
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
   The service now runs using `/opt/discord-blue/.venv/bin/python` to match the
   Python version created by `uv venv`.

**Note**: Make sure to run once to create the config file and input your discord token along with server and bot channel.

```
/root/.local/bin/poetry run discord-blue
```
