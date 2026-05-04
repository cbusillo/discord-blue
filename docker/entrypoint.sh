#!/bin/sh
set -eu

state_dir=/var/lib/discord-blue
runtime_user=discord-blue

if [ -d "$state_dir" ]; then
  state_uid=$(stat -c '%u' "$state_dir")
  state_gid=$(stat -c '%g' "$state_dir")

  if [ "$state_uid" = "0" ] && [ "$state_gid" = "0" ]; then
    chown "$runtime_user:$runtime_user" "$state_dir"
  else
    if ! getent group "$state_gid" >/dev/null; then
      groupmod --gid "$state_gid" "$runtime_user"
    fi
    runtime_group=$(getent group "$state_gid" | cut -d: -f1)
    usermod --uid "$state_uid" --gid "$runtime_group" --home "$state_dir" "$runtime_user"
  fi
fi

install -d -m 700 -o "$runtime_user" -g "$runtime_user" \
  "$state_dir/.config/discord-blue"

exec runuser --user "$runtime_user" -- "$@"
