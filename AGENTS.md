# Project Coding Agent Guide - Discord Blue

## Runtime

* **Python:** 3.13
* **Env manager:** uv

Use `uv run ...` for Python commands so local and Codex Lab runs share the
same managed environment. After `uv sync`, IDEs may point at the repo-local
`.venv/bin/python`; avoid hard-coding `/workspace/...` paths in repo guidance.

Use `uv run mypy .` for static type checking.
Use `uv run ruff check .` for linting.
Use `uv run ruff format .` for formatting.
Use `.github/github.json` for non-secret repo workflow facts,
validation commands, GitHub signal availability, docs routing, important
workflows, and cleanup policy.

## Discord command surfaces

* Prefer Discord slash/app commands for all bot control surfaces. Do not add new
  `!command` style message parsers for operator actions.
* Raw message handling is allowed only when the message content itself is the
  product input, such as agent session-thread replies that are forwarded to
  the local TUI.
* Future agent session control affordances such as status, summary, tail, or
  active-session lookup should be slash/app commands unless there is a product
  reason to make them normal thread replies.

## Tests

* A test must fail when product behaviour breaks and pass when someone makes an
  intended change. Do not write tests that restate the implementation.
* Do not assert a literal that is defined elsewhere (versions, toolchain,
  timeouts, hashes). Import the source of truth, or assert the invariant that
  relates the values (for example, "the finalization budget covers its steps").
* Do not assert workflow or config text. The workflow enforces itself when it
  runs: `ci-gate` fails unless `validate` and `image` pass. Use `actionlint`
  for workflow syntax.
* Verification and loading code must not depend on working-tree state. Tests
  use temporary homes and fakes, never real Discord or production.
