FROM ghcr.io/astral-sh/uv:debian

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies declared in pyproject.toml
COPY pyproject.toml ./
RUN uv pip install --system -r pyproject.toml

# Copy the rest of the project and install in editable mode
COPY . ./
RUN uv pip install --system -e .

# Use uv to run the bot
ENTRYPOINT ["uv", "run", "python", "-m", "discord_blue"]

