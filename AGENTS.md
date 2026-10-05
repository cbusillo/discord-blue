# Project Coding Agent Guide - Discord Blue

## Direction and execution

Read the owner's [overall DIRECTION.md](https://github.com/cbusillo/direction/blob/HEAD/DIRECTION.md)
before working here. This repository has no separate DIRECTION.md; the overall
file owns purpose, work order, stop boundaries, and retired concepts.

AGENTS.md is the repository's only agent-instruction file. Keep product setup
and behavior in README.md and `docs/`, and durable work tracking in GitHub issues.

Follow the maintained [executing loop](https://github.com/cbusillo/codex-skills/blob/main/skills/references/executing-loop.md):
use `github-plan` for issue context, ownership checks, and claims before creating
a linked task worktree; use `github` for task branches, bot commits and pushes,
PRs, and authorized landing. `.github/github.json` records the repository's
validation and landing facts. The current path is a normal merge commit after
green CI; no Launchplane merge train is enabled here.

Changes to these instructions or execution, approval, safety, credential, or
destructive-helper guidance get a review through `model-review`. Weigh its
findings under [reviews by another model](https://github.com/cbusillo/codex-skills/blob/main/skills/references/model-review.md).
The review supplies evidence; reviewer approval is not a merge or completion gate.

## Runtime

* **Python:** 3.13
* **Env manager:** uv

Use `uv run ...` for Python commands so local development and CI use the
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
  `!command` style message parsers for bot control actions.
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
