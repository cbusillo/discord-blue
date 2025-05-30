#!/usr/bin/env bash
set -euo pipefail

# Install system dependencies
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    curl \
    unzip \
    libcairo2 \
    python3.12 \
    python3.12-venv \
    python3.12-dev \
    build-essential

# Install uv package manager
curl -Ls https://astral.sh/uv/install.sh | bash

# Ensure uv is available on PATH
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# Create a virtual environment and install project and development dependencies
uv venv --dev

echo "Setup complete"
