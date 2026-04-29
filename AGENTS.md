# Project Coding Agent Guide - Codex Cloud

## Runtime

* **Python:** 3.13
* **Env manager:** uv

Use `uv run ...` for Python commands so local and Codex Cloud runs share the
same managed environment. After `uv sync`, IDEs may point at the repo-local
`.venv/bin/python`; avoid hard-coding `/workspace/...` paths in repo guidance.

Use `uv run mypy .` for static type checking.
Use `uv run ruff check .` for linting.
Use `uv run ruff format .` for formatting.
Use `.github/github-repo-workflow.json` for non-secret repo workflow facts,
validation commands, GitHub signal availability, docs routing, important
workflows, and cleanup policy.

## Discord command surfaces

* Prefer Discord slash/app commands for all bot control surfaces. Do not add new
  `!command` style message parsers for operator actions.
* Raw message handling is allowed only when the message content itself is the
  product input, such as Every Code session-thread replies that are forwarded to
  the local TUI.
* Future Every Code control affordances such as status, summary, tail, or
  active-session lookup should be slash/app commands unless there is a product
  reason to make them normal thread replies.
