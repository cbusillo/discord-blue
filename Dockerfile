FROM python:3.12-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# Install uv package manager
RUN pip install --no-cache-dir uv

WORKDIR /opt/discord-blue
COPY . .

# Create and populate a uv-managed virtual environment
RUN uv venv /opt/venv && \
    uv pip -p /opt/venv/bin/python install -e .

ENV PATH="/opt/venv/bin:$PATH"

ENTRYPOINT ["/opt/venv/bin/python", "-m", "discord_blue"]

