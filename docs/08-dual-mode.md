# 08 · Dual-mode backend: `router` (fleet) vs `direct` (plain AI Studio)

**Decision (2026-10-09, approved): one gateway, two backends.** Consumers without a
router/VPS must get the same tools, error vocabulary and AX as the fleet — with zero
setup beyond API keys. Ships in 0.8.0 together with the setup wizard (M7.2) and the
npm shim publish.

## Architecture (as built)

The insight that made this cheap: `gemini_router.Router` already talks to
`generativelanguage.googleapis.com` v1beta directly (key rotation, 429 day/minute
classification, Retry-After, model chain, overflow tiers, wait policy, cache). The ONLY
fleet-specific part is the **ledger** (shared SQLite = quota truth across clients).

So `direct` mode is not a second client — it is the same `Router` with a drop-in
**`MemLedger`** (`src/gemini_mcp/backends.py`): identical method contracts and row
shapes (`plan/resolve/rpd_spent/window_usage/cooldown/cache_*/calls_tail/prune/stats`),
in-memory, volatile by design. Protocol parity is by construction — there is no second
code path to drift.

```
AppCfg.backend: auto|direct|router        env GEMINI_MCP_BACKEND overrides
resolve_mode(): auto → 'router' iff GEMINI_ROUTER_CONFIG set or state_path exists,
                else 'direct'  (consumer default; fleet configs pin backend: router)
make_routers(): → (router, router_optimal, mode); direct mode shares ONE MemLedger
                between both instances so in-process counting stays coherent
```

## Semantics per mode

| Aspect | router (fleet) | direct (consumer) |
|---|---|---|
| Quota truth | shared SQLite ledger, all clients | per-process approximation, resets on restart |
| Disk state | state.db (calls, cache, cooldowns) | none — `MemLedger.path == ""` |
| Cache / idempotency | survives restarts (24h TTL) | per-process only |
| `gemini-mcp calls` | persistent audit tail | empty + honest note (no persistent log) |
| `gemini-mcp quota` | live shared table | cold view of limits + note; live numbers via `quota_status` tool / `meta.quota_after` |
| `prune` / `doctor` | full (SQLite) | jsonl/audit only; doctor skips state, probes listModels |
| Multi-client fairness | enforced (shared gates) | NOT enforced — one user, one process |

## AX parity (non-negotiables, all shipped)

- Identical tool set, error family (`quota_exhausted`, `plan_budget_insufficient`),
  `RouterEvent` progress stages, meta shape.
- `meta.backend` + `report` line now starts with `backend=direct|router` — the agent
  always knows which mode answered (report travels in the payload because clients
  truncate tool descriptions, p01).
- `quota_status` adds `counters:` label — "shared ledger (fleet truth)" vs
  "approximate: in-memory per process" (honesty over symmetry).
- `/healthz` and CLI startup logs carry `backend=`.

## Keys & config

Direct mode reads keys through the existing `RouterConfig.load()` plumbing:
`GEMINI_API_KEYS` / `GEMINI_API_KEY_1..10` env (wizard will write them to
`~/.config/gemini-mcp/.env`). No new key config surface — deliberate.

## Tests

`tests/test_direct_backend.py` (8): MemLedger contract parity (planned-vs-ok windows,
count-rows never spend RPD, cooldown MAX, cache TTL/upsert, prune/stats), mode
resolution (auto/env/state-file), shared-ledger invariant, and respx E2E against a
mocked AI Studio: happy path + RPD decrement, 429-per-day → model switch, countTokens,
`backend=direct` in report/meta. Suite: 71 green.

## Follow-ups

- M7.2 setup wizard — **DONE (0.8.0)**: `gemini-mcp setup` asks the one fleet-vs-simple
  question, writes `~/.config/gemini-mcp/{config.yaml,.env 0600}`, probes keys live,
  prints client snippets (direct = consumer default). npm publish `@vernikr/gemini-mcp`
  0.8.0 is the remaining step.
- Fleet VPS config.yaml: add explicit `backend: router` at next deploy (auto-detect
  already resolves correctly there via state.db, but pinning removes the heuristic).
