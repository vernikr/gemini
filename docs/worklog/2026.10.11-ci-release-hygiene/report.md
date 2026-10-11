# Item B closed: a release no longer runs the same checks twice

Released as **gemini-mcp 0.17.3** and **gemini-router 0.1.11** — both carry no functional
change; they exist to exercise the reworked pipeline on a real tag.

## What changed

- `ci.yml` in both repos: `on: push: branches: [main]` + `pull_request`. Tags matched the
  old `on: [push]`, so every release ran `ci.test` next to `release.test` over one commit.
  Feature branches stay covered through PRs.
- `release.yml` (mcp) smoke: polls the registry index for the version — one `curl | jq -e`
  per try, 10 s apart, 10-minute ceiling per registry — then installs once, when the
  install can only succeed. It used to retry the *install* (12×30 s PyPI, 16×30 s npm),
  paying a full dependency re-resolve for every 30 s of propagation.
- `--refresh` stays on the uvx call: the polled JSON API and uv's cached simple index are
  different endpoints.

Correction to the parent plan: it credited the router with a standalone `uvx ruff` lint job
"already done". That describes gemini-mcp; the router runs one job where a single
`uv sync --extra dev` serves lint and tests, so there was nothing to split there.

## Evidence

Run history on the tags (from the GitHub API, not from logs):

| | v0.17.2 (before) | v0.17.3 (after) |
|---|---|---|
| workflows on the tag | `release` **+** `ci` | `release` only |
| release wall clock | 163 s | 136 s |
| smoke job | 106 s | 75 s |

Same shape in the router: `ci v0.1.10` + `release v0.1.10` before, `release v0.1.11` alone
after. Both new tags are green end to end.

The typical path is roughly unchanged — both runs answered on their first attempt — so the
win is bounded on the *slow* side: a lagging registry no longer costs a re-resolve per
30 s of waiting, and the ceiling is a clear 10-minute poll instead of ~14 minutes of loop.
The smoke body was also extracted and run locally against the live registries before the
commit: a published version answers the poll immediately and both installs print it; an
impossible version exits 1 with the intended `::error::` line.

## Live state after the chunk

PyPI and npm serve 0.17.3, PyPI serves router 0.1.11, and the VPS daemon reports 0.17.3
with `gemini-mcp.service` active. The external network services units were not touched and are active.

## Docs

`docs/09-ci.md`: the quirks section described an `8×30s` retry the workflow had already
outgrown — replaced with the poll-then-install rule and why `--refresh` survives; the
trigger split is stated in the intro.
