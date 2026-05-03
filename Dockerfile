FROM ghcr.io/astral-sh/uv:debian

WORKDIR /app

COPY . ./
RUN uv sync --all-groups --python 3.13

ENTRYPOINT ["uv", "run", "discord-blue"]
