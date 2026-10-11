# Chunk 2 of `2026.10.11-remaining-work`: item B — CI does redundant work on every release

Scope: `.github/workflows/` in both repos. No source, no behaviour change.

## What was wrong

- `ci.yml` in both repos triggered on `on: [push, pull_request]`, tags included, so every
  release ran `ci.test` **and** `release.test` over the same commit. Confirmed in the run
  history: `ci v0.17.2` + `release v0.17.2` on one SHA (same in the router).
- The mcp `smoke` job retried the *install*: 12 × 30 s for PyPI, 16 × 30 s for npm, each
  attempt a full `uvx --refresh` / `npx -y` re-resolve. Worst case ~14 min of loop, and
  the propagation wait was paid with dependency work nobody needed yet.

Correction to the parent plan: it says the router's lint is "already its own `uvx ruff`
job with no dependency install". That is true of gemini-mcp's `ci.yml`, not the router's —
the router runs one job where `uv sync --extra dev` serves both lint and tests, so there
was nothing to split. Only the trigger scoping applies there.

## Fix

1. Both `ci.yml` files: `on: push: branches: [main]` + `pull_request`. A tag matches
   neither (the tag push is the release workflow's business), feature branches stay
   covered through PRs.
2. `release.yml` smoke: poll the registry index for the version (one `curl | jq -e` per
   try, 10 s apart, 10 min ceiling per registry), then install once. `--refresh` stays on
   the install: the JSON API that was polled and uv's cached simple index are different
   endpoints.

## Verification

- The smoke body was extracted and run locally against the live registries with the
  already-published version: polling answered immediately, both installs printed the
  version, exit 0. With an impossible version (loop shortened for the test) it exits 1
  with `::error::PyPI did not serve …`.
- Both YAML files parse; job graphs unchanged apart from the stated intent.
- Tag history after this chunk is the real proof: see the report.

## Publication

`ci:` commit + push in both repos, then an empty patch release in each
(`gemini-mcp 0.17.3`, `gemini-router 0.1.11`). No functional delta — the release exists to
exercise the rewritten pipeline: the tag must trigger `release` only, and `smoke` must
reach both installs without a single re-resolve loop.
