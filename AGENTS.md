# Project Coding Agent Guide - Discord Blue

## What Discord Blue does

Discord Blue is a Discord bot with a plugin system and an agent-session bridge.
The Director uses Discord threads to interact with local Codex and Claude Code sessions.
Evidence: [README.md](README.md).

## Direction and execution

Read the owner's [overall DIRECTION.md](https://github.com/cbusillo/direction/blob/HEAD/DIRECTION.md)
before working here. This repository has no separate DIRECTION.md; the overall
file owns purpose, work order, stop boundaries, and retired concepts.

AGENTS.md is the repository's only agent-instruction file. Keep product setup
and behavior in README.md and `docs/`.

Follow the maintained [executing loop](https://github.com/cbusillo/codex-skills/blob/main/skills/references/executing-loop.md).

Use `model-review` for changes to AGENTS.md and whenever the maintained
[reviews by another model](https://github.com/cbusillo/codex-skills/blob/main/skills/references/model-review.md)
criteria call for it. That reference also owns how findings are weighed.

## Runtime

* **Python:** 3.13
* **Env manager:** uv

After `uv sync`, IDEs may point at the repo-local
`.venv/bin/python`; avoid hard-coding `/workspace/...` paths in repo guidance.

Use `uv run mypy .` for static type checking.
Use `uv run ruff check .` for linting.
Use `uv run ruff format .` for formatting.
[Workflow metadata](.github/github.json) records repository commands and gates.

## Discord command surfaces

* Prefer Discord slash/app commands for all bot control surfaces. Do not add new
  `!command` style message parsers for bot control actions.
* Raw message handling is allowed only when the message content itself is the
  product input, such as agent session-thread replies that are forwarded to
  the local TUI.
* Future agent session control affordances such as status, summary, tail, or
  active-session lookup should be slash/app commands unless there is a product
  reason to make them normal thread replies.

## Tests

Follow the [shared test rules](https://github.com/cbusillo/codex-skills/blob/main/instructions/global.md#tests).

* `ci-gate` fails unless `validate` and `image` pass. Use `actionlint`
  for workflow syntax.
* Loading code must not depend on working-tree state.
* Tests use temporary homes and fakes, never real Discord or production.
