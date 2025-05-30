# Project Coding Agent Guide — Codex Cloud

## Runtime

* **OS:** Ubuntu 24.04 (noble)
* **Python:** 3.13
* **Env manager:** uv

use `/workspace/discord-blue/.venv/bin/python` as the interpreter
use `uv run mypy .` for static type checking
use `uv run ruff check .` for linting
use `uv run ruff format .` for formatting