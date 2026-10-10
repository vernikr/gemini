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

So `direct` mode is not a second client — it is the same `Router` engine, differing only
in WHERE its ledger lives. **0.8.0** shipped direct with a volatile in-memory ledger;
**0.8.1 (owner decision): direct persists to a LOCAL SQLite ledger** under the config
home (`~/.config/gemini-mcp/state.db`) — usage survives restarts and is coherent across
every gateway process on the machine (Freebuff + Claude Desktop running at once). It is
still per-machine: cross-machine truth is what fleet/router mode exists for.
`MemLedger` (`src/gemini_mcp/backends.py`, same method contracts and row shapes as the
sqlite `Ledger`) remains for side-effect-free live key probes (`setup`, no disk writes,
no RPD spend). Protocol parity is by construction — there is no second code path to drift.

```
AppCfg.backend: auto|direct|router        env GEMINI_MCP_BACKEND overrides
resolve_mode(): auto → 'router' iff GEMINI_ROUTER_CONFIG set or state_path exists,
                else 'direct'  (consumer default; fleet configs pin backend: router)
make_routers(): → (router, router_optimal, mode); direct mode points BOTH instances at
                one local state file (direct_state_path(): explicit state_path wins,
                else <config home>/state.db) — coherent counting across processes
```

## Semantics per mode

| Aspect | router (fleet) | direct (consumer, 0.8.1) |
|---|---|---|
| Quota truth | shared SQLite ledger, all clients/machines | local SQLite ledger — accurate on this machine, not shared across machines |
| Disk state | VPS `state.db` (calls, cache, cooldowns) | `~/.config/gemini-mcp/state.db` (+ logs under the same home); removed by `uninstall` |
| Cache / idempotency | survives restarts (24h TTL) | same — survives restarts (local file) |
| `gemini-mcp calls` | persistent audit tail | persistent LOCAL audit tail (live rows) |
| `gemini-mcp quota` | live shared table | live local table + ledger path note |
| `prune` / `doctor` | full (SQLite) | full (local SQLite); doctor prints local state stats + listModels probe |
| Multi-client fairness | enforced across machines | enforced across processes on ONE machine |

## AX parity (non-negotiables, all shipped)

- Identical tool set, error family (`quota_exhausted`, `plan_budget_insufficient`),
  `RouterEvent` progress stages, meta shape.
- `meta.backend` + `report` line now starts with `backend=direct|router` — the agent
  always knows which mode answered (report travels in the payload because clients
  truncate tool descriptions, p01).
- `quota_status` adds `counters:` label — "shared ledger (fleet truth)" vs
  "local persistent ledger — accurate on this machine, not shared across machines"
  (honesty over symmetry).
- `/healthz` and CLI startup logs carry `backend=`.

## Keys & config

Direct mode reads keys through the existing `RouterConfig.load()` plumbing:
`GEMINI_API_KEYS` / `GEMINI_API_KEY_1..10` env (wizard writes them to
`~/.config/gemini-mcp/.env`). Since 0.9.0 also: `--keys` / `--keys-file` global CLI
flags, `GEMINI_MCP_KEYS_FILE` env, and `router.keys_file` in config.yaml (precedence
in that order, file format: comma/whitespace/newline + `#` comments) — see
docs/06-consumer.md. No other new key surface — deliberate.

## Tests

`tests/test_direct_backend.py` (8): MemLedger contract parity (planned-vs-ok windows,
count-rows never spend RPD, cooldown MAX, cache TTL/upsert, prune/stats), mode
resolution (auto/env/state-file), LOCAL-LEDGER PERSISTENCE (spend survives a simulated
restart, both routers share one state file), and respx E2E against a mocked AI Studio:
happy path + RPD decrement, 429-per-day → model switch, countTokens, `backend=direct`
in report/meta. Suite: 80 green (incl. 9 wizard tests).

## Follow-ups

- M7.2 setup wizard — **DONE (0.8.0)**, npm `@vernikr/gemini-mcp` 0.8.0 — **PUBLISHED**
  (both stores live 2026-10-10, clean-sandbox E2E verified).
- **0.8.1**: direct-mode local persistence (this doc, owner request) + CI dual-publish
  (M7.3).
- Fleet VPS config.yaml: add explicit `backend: router` at next deploy (auto-detect
  already resolves correctly there via state.db, but pinning removes the heuristic).
