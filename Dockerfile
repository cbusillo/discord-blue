FROM ghcr.io/astral-sh/uv:debian

WORKDIR /app

COPY . ./
RUN uv sync

ENTRYPOINT ["uv", "run", "discord_blue"]