FROM ghcr.io/astral-sh/uv:debian

RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN uv pip install --system -r pyproject.toml

COPY . ./
RUN uv pip install --system -e .

ENTRYPOINT ["uv", "run", "python", "-m", "discord_blue"]

