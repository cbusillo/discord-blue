#!/usr/bin/env bash
set -euo pipefail

curl -Ls https://astral.sh/uv/install.sh | bash

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

uv sync

echo "Setup complete"
