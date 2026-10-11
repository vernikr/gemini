# Tails closed, and the hand-off state

Written at the end of 2026-10-11 after the owner added a fresh key and asked for the loose
ends to be tidied and the plan brought up to date before starting C2 on a new branch.

## 1 · The fresh key is in, and the pool is clean

`gemini-mcp keys add` accepted `AQ.Ab8…foBA` (fingerprint `47150d98`) with a live probe of
`ok` — the standard `x-goog-api-key` transport, no fallback needed. The two dead keys were
removed with the command that already existed (`keys remove b41654f0 79832f6b`), so the
pool is one living key. The owner's question "shouldn't there be a removal command?" was
answered by the command, and by 0.20.4 for the discoverability gap: `keys list --check`
now names the rejected keys and prints the exact removal command (`--json` gains
`rejected`), plain `keys list` teaches `--check` and `remove`, and the post-`add` pointer
mentions removal too.

## 2 · The high-resolution PDF default on the live server

The owed demonstration, and its honest boundary.

Verified live on the VPS (daemon 0.20.3 at the time, key `47150d98`):

```
[  4%] three.pdf: 727 bytes → provider Files API (application/pdf)
[  8%] provider file files/wzdq6u1mblw7 ready · 3360 tokens (high)
```

A 3-page text PDF measures exactly 3 × 1120 = 3360 tokens at `high` — the level is applied
by default, the count comes from the provider's own `countTokens`, and the number matches
the tariff table. That closes the specific debt carried since 2026-10-11 morning.

Not closed: a successful *generation* over that file. The provider answered 500 to every
file-bearing `generateContent` on the models that can read files (3.8-flash: 3 × 500,
retried by the router), 400 to the models that cannot (preview, gemma), and exhausted the
per-day quota of the remaining candidate (3.6-flash). Text prompts to the same models
succeeded throughout, and `countTokens` with the same file part succeeded — so the failure
sits in the provider's file path at this hour plus this account's free-tier budget, not in
our payload. Provisional status, to re-check after the PT-day reset (07:00 UTC): a media
job should then complete on the chain's flash model. Evidence: `logs/app.log` 13:33–13:58
UTC and the direct probes in this session.

## 3 · What the demo found and fixed (both released)

1. **File-blind models were being dispatched.** Router 0.1.15: a file job never enters the
   overflow tiers, a model that answers 400/500 to file input is remembered as
   file-incapable for the process, and a file-blind candidate set produces its own error
   instead of "quota exhausted". Live proof: after 0.20.5 the log has no `overflow → tier`
   line for a file job, and the quota hint names the file-capable constraint.
2. **A quota-drained chain blamed the key.** The same tests exposed the pin clause firing
   on a 429-drained run — "re-upload the file under a key that can" — which is wrong and
   sends the operator hunting for a healthy key they already have. The clause now applies
   only when the key itself cannot serve the file-capable models.
3. **The dependency floor never actually moved.** On the VPS the daemon ran
   gemini-mcp-gateway 0.20.4 *with router 0.1.14*: the pin bumps for 0.20.2 (keyauth) and
   0.20.4 (file-capable jobs) were string replacements that never matched the line — it
   carries a `,<0.2` the pattern omitted — so three releases declared
   `gemini-router>=0.1.13` while requiring newer behaviour. 0.20.5 sets `>=0.1.15`, and
   `tests/test_packaging.py` compares the declared floor against the capability floor, so a
   silent miss now fails the suite. Verified live: the VPS reports 0.20.5 + router 0.1.15.

## 4 · Releases and where things stand

| artefact | version | where |
|---|---|---|
| gemini-mcp-gateway | **0.20.5** | PyPI, npm, VPS (unit active) |
| gemini-router | **0.1.15** | PyPI, and on the VPS as the gateway's dependency |
| key pool | 1 key (`47150d98`) | `/opt/apps/gemini-mcp/.env`, managed by `gemini-mcp keys` |
| CLI on the VPS | on PATH everywhere | `/usr/local/bin/gemini-mcp` (shim into the uv tool bin) |

Five releases went out today after the incident (0.20.1 → 0.20.5, router 0.1.14 → 0.1.15);
every one ran the full five-job pipeline green, including the rewritten consumer smoke. The
two failures along the way (0.20.1's test job, router 0.1.14's lint) were both "tagged
before the battery finished" and both ended in a re-tag — the battery is pytest +
`ruff check .` + the secrets guard, in that order, before any tag.

## 5 · Hand-off

`docs/worklog/2026.10.11-remaining-work/plan.md` is the entry point: it now carries a
"Hand-off — start here" section (state of the world, the C2 brief with today's measured
constraints, the owner's binding decisions, the publication procedure, what not to redo),
and the C2 section lists what the live session taught. The first branch to cut is
`feat/c2-group-packing`; the plans it needs are
`docs/worklog/2026.10.11-binary-file-support/plan.md` (product),
`gemini-router → docs/worklog/2026.10.11-binary-file-support/plan.md` (capability) and
`gemini-router → docs/worklog/2026.10.11-media-tariff-forecast/plan.md` (forecast + drift,
with the corrected mail path: Gmail SMTP from GitHub Actions, not from the VPS).
