# 0.19.0 and 0.20.0 — media levels, and a CLI that helps instead of scolding

Owed worklog, written on 2026-10-11 at the end of the day; the changes it describes
shipped earlier the same day. The capability half of the media work is a separate library
change, recorded in `gemini-router → docs/worklog/2026.10.11-media-resolution-api/`.

## 0.19.0 — the `keys` command and media plumbing

- `gemini-mcp keys` / `keys add` / `keys remove` / `keys list --check`: one command to
  manage the key pool wherever the gateway runs. It finds the pool file itself (the
  systemd unit's `EnvironmentFile` on a server, the wizard's `.env` on a workstation),
  validates a key before anything is written, probes it live (`listModels` — free, no RPD)
  unless `--offline`, writes atomically at 0600, and restarts the unit when there is one.
  A key that fails the probe is refused unless `--force`; the last key cannot be removed
  without `--force`; nothing ever prints a raw key.
- Media plumbing that later carried the resolution default: `ingest.media_resolution`,
  the "uploaded (…)" stage line, `meta.file.resolution`, and reusing a provider file
  reference with the level of the current call.

## 0.20.0 — every media type reads at HIGH, and mistakes get an answer

**Media level (owner decision, 2026-10-11).** Images were already HIGH by default; PDFs
moved from the provider default (medium, 560 tokens/page) to HIGH (1120/page). Accepted
as cheap next to the per-minute limits, and it removes a decision from the caller:
`ingest.media_resolution: high` is the default for every media type, validated at load,
`""` keeps the provider default as the escape hatch. Measured the same day on
`gemini-3.6-flash`: image 256/529/1089 tokens at low/medium/high; PDF page 280/560/1120.
The level rides into both the `countTokens` measurement and the analysis request — a
measurement at one level and a send at another would make the gate and the ledger argue.
Live verification of the high-resolution path itself happened later (see
`2026.10.11-tails-and-handoff/report.md`: 727-byte, 3-page PDF → 3360 tokens at high).

**CLI friendliness (owner decision, 2026-10-11).** Bare `gemini-mcp` prints the command
map and one place to start (`gemini-mcp setup`, or `doctor` when already wired) and exits
0 — a bare invocation is a question, not an error. A typo answers with the usage block and
`did you mean 'X'?` (nearest command, flag or positional) and exits 2. Six tests pin the
behaviour; `capsys` cannot see rich output, so the bare-invocation path prints plainly.

Also in 0.20.0: `docs/02` FR-B11 for the media default, `docs/06` for the consumer view,
and the release pipeline's smoke job rewritten after the `uvx` failure of the first run
(the PyPI JSON API and uv's view of `/simple/` are separate caches; the job now retries
the real consumer commands instead of polling metadata).
