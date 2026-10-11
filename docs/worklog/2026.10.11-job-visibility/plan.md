# G · Job visibility on the polling surface · done 2026-10-11

ТЗ from `2026.10.11-remaining-work/plan.md` §G, resolved polling-first (the wave
decision): the harness's progress-notification support is unknown, so the status rides
the surface every agent already uses — the running ticket.

## Shipped (gateway 0.23.0)

- `ProgressEmitter(ctx, logger, live=dict)` mirrors its latest state into a plain dict
  shared with JobManager: canonical `phase` from the router event vocabulary
  (`waiting`+reason → `waiting_rpm`/`waiting_tpm`/`waiting_cooldown`; `dispatch` →
  `generating`; `retry` → `retrying`; `switch` → `switching_model`) and from gateway
  stages (`uploading/measuring/packing/chunking/consolidating/finishing`), `pct`,
  latest stage line, `model`, `key_id`, `attempt`, and `wait_until` while a gate is
  being waited out.
- `_jobbed` creates the dict per async call; `JobManager.submit(..., live=)` attaches
  it (seed `phase: starting`). Every running ticket carries it as `live`, `eta_s`
  recomputed at read time (counts down; never negative). Done/error/unknown tickets
  unchanged. No new tool, no subscription, no schema change for final results.
- Docs: FR-E10 (02), §4 live-status paragraph (03), §3 poller guidance (06).
- Tests: tests/test_jobs.py (ticket carries live from submit; router event → phase map
  + eta; gateway stage clears eta; runner stages visible mid-poll).

## Open question carried forward (not blocking)

Does the host harness surface MCP progress notifications? If yes, `live` is still the
floor for pollers; no action until a harness is known to support them.
