# Wave 1 gateway half closed: the cost drift signal on the provider path

Released as **gemini-mcp-gateway 0.21.0** (PyPI + npm + VPS). Plan in this folder; the
library half and its evidence live in
`vernikr/gemini-router → docs/worklog/2026.10.11-media-tariff-forecast/report.md`.

## What changed

- `analyze_file`'s provider path predicts the cost after the free measurement:
  `forecast_media_tokens(mime, pages, level=<the level the request sends>)`. A pdf with
  unknown page count gets no prediction; unmeasured classes are skipped.
- On a miss beyond the noise floor the result gains
  `meta.cost.drift = {predicted, measured, pct}` and the report gains one `COST DRIFT`
  line that doubles as an instruction to tell the owner; the miss is written through
  `router.ledger.record_drift` (source='mcp'), so `gemini-mcp doctor` and `quota` carry
  the router CLI's one-line signal with no gateway code.
- `_Loaded` carries the pre-flight page count; nothing else in the file flow moved.
- Docs: FR-B12 added; 02/03/06 no longer restate a single tariff number — the router's
  table is the only copy (the rule this wave was written under).

## Evidence

- Tests 176 → 179: drift fires on an image measured 2000 against the 1089 forecast
  (`meta.cost.drift` exact, report line present, one ledger row with class/level/predicted
  readable back); an exact match leaves no `cost` key and no row; a page-less pdf is
  silent. The fake router now carries a real in-memory ledger — the record path is
  exercised against the genuine surface, not a stub.
- CI simulation before tagging: `UV_NO_SOURCES=1 uv pip install -e ".[dev]"` resolved
  router 0.2.0 from PyPI; battery green against the published wheel.
- Tag `v0.21.0`: all five release jobs green (test → PyPI → npm → deploy → smoke);
  PyPI and npm serve 0.21.0; the VPS daemon reports `gemini-mcp 0.21.0` with the unit
  active.
