# Every key generation, everywhere

Field report that started this: the operator tried `gemini-mcp keys add <key>` on the VPS
and got `command not found`; and the question behind it — "why did the `AQ.…` key work
before, and can we please make every key type just work?" — deserved an answer with
evidence rather than a restriction.

## What was actually true about `AQ.…` keys

`AQ.…` is not a second-class key. In this project's short history it authenticated over the
ordinary `x-goog-api-key` header: the pool's spare key passed the live `listModels` probe
on the VPS on 2026-10-11 11:07 UTC, and it was the *same* 12:45 leak sweep that killed it
along with the `AIza…` key (401 ever since, on every transport we tried). The forum reports
of `AQ.…` 401s did not apply to our surface — but they are the reason the code must not
assume either.

## The fix, in three places

1. **Library (`gemini-router` 0.1.14, SPEC §10).** Every provider call goes through a new
   `keyauth` layer: an ordered transport list (`header`, `query`, `bearer`), starting with
   the transport that last worked for that key. It rotates only on an auth-shaped refusal —
   401 always; 400/403 only when the body is about keys or credentials, so a model
   permission 403 keeps its model-switch meaning and costs nothing extra — and remembers
   the winner per key, so a healthy pool behaves exactly as before (one header request,
   no probing). A leaked-key block is never rotated and now arrives as `KeyLeakedError`,
   the cause the operator needed hours earlier on 2026-10-11. `probe_key()` (free,
   `listModels`) exposes the same logic to CLIs.
2. **Product (`gemini-mcp` 0.20.2).** `keys add` / `keys list --check` delegate to
   `keyauth.probe_key` — one implementation, so the CLI's verdict and the daemon's
   behaviour cannot drift — and print the transport when it is not the default
   (`ok (query)`), while a healthy key stays a quiet `ok`. Log redaction learned the `AQ.…`
   shape: it knew only `AIza…`, so an AI Studio key could have reached a log in clear
   text. Docs and requirement FR-C6 state the rule: key shape is never a gate.
3. **Host.** The VPS now has `/usr/local/bin/gemini-mcp` (and the `browser-agent` sibling)
   pointing into the uv tool bin, so the command works for root in an interactive login
   *and* in a non-interactive `ssh host cmd` session — PATH setup had lived only in the
   deploy tooling's own environment, which is exactly the kind of "works for me" that fails
   for a human a month later.

## Verification

- Router: 76 → 86 tests, including a rotation that is remembered, an upload that needs a
  second transport, a leaked key that produces exactly one provider call, and a
  model-permission 403 that must not rotate. `ruff check .` clean (whole repo now linted by
  both workflows, not just `src tests`).
- Package: 167 → 172 tests (AQ. redaction; transport-aware probe rendering; `ok (query)`
  must not read as a failure to scripts).
- Live: PyPI serves `gemini-router` 0.1.14; the release run for `v0.20.2` gates PyPI, npm
  and the VPS deploy.

## Outstanding

- **The pool itself is still empty** — both keys were revoked in the leak incident. A fresh
  key (either generation) added with `gemini-mcp keys add <key>` is the only remaining
  step; the command now validates over the transports that matter and will say
  `ok (query)`/`ok (bearer)` if it ever needs one.
- The first live `AQ.…` probe after the rotation settles the remaining question — whether
  a *fresh* AI Studio key still takes the plain header. If it does, the fallback is a
  safety net rather than a daily path; if it does not, the operator never notices.
