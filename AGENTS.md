# Project Coding Agent Guide — Codex Cloud

## Runtime

* **OS:** Ubuntu 24.04 (noble)
* **Python:** 3.13
* **Env manager:** uv

use `/workspace/discord-blue/.venv/bin/python` as the interpreter
use `uv run mypy .` for static type checking
use `uv run ruff check .` for linting
use `uv run ruff format .` for formatting

## Discord command surfaces

- Prefer Discord slash/app commands for all bot control surfaces. Do not add new `!command` style message parsers for operator actions.
- Raw message handling is allowed only when the message content itself is the product input, such as Every Code session-thread replies that are forwarded to the local TUI.
- Future Every Code control affordances such as status, summary, tail, or active-session lookup should be slash/app commands unless there is a product reason to make them normal thread replies.
