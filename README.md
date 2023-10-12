# Discord Blue Installation Guide

Discord Blue is a new basic discord bot plugin system. This guide provides step-by-step instructions for setting it up on
Debian-based systems.

## Prerequisites

Ensure you have \`git\`, \`curl\`, and \`python3\` installed.

```
apt update
apt install git curl python3
```

## Installation Steps

1. Update System Packages:
   ```
   apt update
   ```

2. Navigate to the /opt Directory:
   ```
   cd /opt
   ```

3. Install Python Poetry:
   ```
   curl -sSL https://install.python-poetry.org | python3 -
   ```

4. Clone the Discord Blue Repository:
   ```
   git clone https://github.com/cbusillo/discord-blue
   cd discord-blue
   ```

5. Install Dependencies using Poetry:
   ```
   poetry install
   ```

6. Setup and Start the Systemd Service:
   ```
   cp discord-blue.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable discord-blue
   systemctl start discord-blue
   ```

**Note**: Always ensure you're running trusted scripts, especially with piped commands like \`curl | python3\`.
