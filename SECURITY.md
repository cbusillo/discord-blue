# Security Policy

## Supported Versions

Security fixes target the current `main` branch. There are no separate
supported release lines.

## Reporting a Vulnerability

Report suspected vulnerabilities privately through GitHub's
[Report a vulnerability](https://github.com/cbusillo/discord-blue/security/advisories/new)
form. Do not open a public issue for a vulnerability.

Include the commit, how the bot is run (container or systemd), the impact,
and the smallest steps that reproduce it.

Do not send bot tokens, Discord user or server IDs, session transcripts, host
names, or other personal data. Use redacted or made-up values.

This is a single-maintainer project. Reports are handled on a best-effort
basis, and I aim to reply within seven days.

## Scope

Relevant reports include:

- someone without access sending input to, or reading output from, a
  connected agent session;
- bot tokens or other credentials leaking into logs, messages, or state
  files;
- plugins or bridge messages running code or commands they should not; and
- the Docker image, systemd unit, or GitHub Actions supply chain.

Problems in Discord or in discord.py should go to those projects.
