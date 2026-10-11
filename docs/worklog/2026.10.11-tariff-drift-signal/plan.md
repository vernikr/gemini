# Wave 1, gateway half: consume the tariff forecast, surface the drift signal

The library half — the tariff table, `forecast_media_tokens`, `check_drift`, the ledger
`drift` kind and the doctor/quota line — lives and is documented in
`vernikr/gemini-router → docs/worklog/2026.10.11-media-tariff-forecast/`. This folder
states only what the gateway does with it. Sequencing: §Wave plan in
`docs/worklog/2026.10.11-remaining-work/plan.md`.

## What the gateway does

1. **Predict before the call.** On the provider path of `analyze_file`, the gateway calls
   `forecast_media_tokens(mime, pages=…, level=<the level the request will send>)` after
   the free measurement. A pdf with no page count gets no prediction — a wrong forecast is
   worse than none. Unmeasured classes (audio/video) raise and are simply skipped.
2. **Signal in-band.** When `check_drift` fires, the result gains
   `meta.cost.drift = {predicted, measured, pct}` and the report gains one `COST DRIFT`
   line that doubles as an instruction to tell the owner. Absent field = no drift.
3. **Record for the operator.** The miss is written through `router.ledger.record_drift`
   (kind='drift', non-spending, source='mcp'); `gemini-mcp doctor` and `quota` inherit the
   router CLI's one-line signal automatically.

## Rules kept

- The prices live only in the router's table; no gateway doc restates a number (the
  02/03/06 edits of this wave removed the copies that existed).
- A miss never fails the request and changes no accounting.
- Text documents are untouched — the signal belongs to the provider path only.

## Tests

Three in `tests/test_files_api.py`: drift fires, is reported and lands in the ledger;
matching measurement leaves no `cost` key; a page-less pdf gets no forecast. The fake
router now carries a real in-memory ledger, so the record path is exercised against the
genuine surface.
