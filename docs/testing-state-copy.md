# Offline testing state copies

`python -m discord_blue.testing_state_copy` is the image's offline state-copy
interface. It imports no bot or session runner and makes no network requests.
Launchplane owns source qualification, separate storage, immutable image
selection, operation recovery and application activation. This interface does
not grant a production restore or make an unqualified source safe.

Run it in the selected immutable product image with the source state mounted
read-only and a separate, freshly allocated destination. Pass no runtime
environment, tokens, source credentials or network access. The image entrypoint
can prepare the destination's empty `.config/discord-blue` directories before
running the utility as its runtime user. Existing files are never overwritten.

The three commands are:

```text
python -m discord_blue.testing_state_copy plan --source /source
python -m discord_blue.testing_state_copy copy --source /source --destination /var/lib/discord-blue --expected-sha256 <planned-sha256>
python -m discord_blue.testing_state_copy verify --destination /var/lib/discord-blue --expected-sha256 <planned-sha256>
```

Each successful command returns one JSON object with `status`, `format`,
`sha256` and `byte_count`. The format identifies the product-owned copy
contract. The digest covers the exact sanitized config bytes. Keep the planned
receipt with the original operation before copying. Changed preferences refuse
before writing. A lost copy response is settled with `verify` and the original
digest, without copying over an existing destination.

Only `debug`, `agent_session.heartbeat_timeout_seconds` and
`agent_session.heartbeat_check_interval_seconds` survive. Their types are
validated; heartbeat values must be positive integers. Everything else is
omitted, including tokens, guild/channel IDs, roles, plugin declarations,
conversation bindings, session recovery, bridge delivery and handover files.
Unknown fields and files cannot introduce another outbound connection into the
copy. An absent configuration produces empty preferences. Invalid TOML or
invalid retained preferences refuse the copy.

Independent verification requires the exact sanitized tree and allows no other
config fields or files. Symbolic links, overlapping source/destination paths,
nonregular or oversized config files, and populated destinations refuse.
Failures return a bounded refusal result without config contents or paths.
The source is never written. Interrupted destination writes remain in custody;
verification must pass before the application starts.

After verification, normal activation supplies the independent testing lane's
managed settings and credentials. The copied preferences cannot start a
production connection, and the application is not started by this utility.
