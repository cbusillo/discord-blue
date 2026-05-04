FROM ghcr.io/astral-sh/uv:debian

WORKDIR /app

ENV HOME=/var/lib/discord-blue
ENV PYTHONUNBUFFERED=1

COPY . ./
RUN uv sync --frozen --no-dev --python 3.13 \
    && groupadd --system discord-blue \
    && useradd --system --home-dir /var/lib/discord-blue --no-create-home \
        --gid discord-blue --shell /usr/sbin/nologin discord-blue \
    && install -d -m 700 -o discord-blue -g discord-blue \
        /var/lib/discord-blue/.config/discord-blue \
    && install -m 755 docker/entrypoint.sh /usr/local/bin/discord-blue-entrypoint \
    && chown -R discord-blue:discord-blue /app

VOLUME ["/var/lib/discord-blue"]
EXPOSE 8787

ENTRYPOINT ["/usr/local/bin/discord-blue-entrypoint"]
CMD ["/app/.venv/bin/discord-blue"]
