# 02 · Functional & Business Requirements — Stage 2 artifact

Date: 2026-10-08 · Inputs: `worklog/archive/01-unpacking-reframing.md` ·
Priority: MUST / SHOULD / MAY.
Numeric defaults live in `config.yaml` (single source); this doc references key names, not values.

## A. Business requirements

| ID | Requirement | Success measure |
|---|---|---|
| BR1 | The Freebuff agent offloads massive-body analysis to the gateway and receives only a compact consolidated result — its own context stays clean | Result body ≤ `max_result_chars`; no intermediate chunk texts in the tool return |
| BR2 | Quota is never spent blind: every request is pre-checked against remaining RPD/RPM/TPM, and every spend is persisted | Zero unexplained 429s in the audit log; ledger survives restarts |
| BR3 | "Which model can I send to right now?" is answerable in one command / one MCP call, by any consumer | `quota_status` tool + `gemini-router quota` CLI show per key×model: RPD left, RPM/TPM window state, cooldowns, reset ETA, recommended target |
| BR4 | The router is reusable outside this stack (Python dep, REST, MCP) | `uv add git+…/gemini-router.git@vX` works in a foreign project; REST `/v1/*` documented |
| BR5 | `external-consumer` and the gateway coexist on the same key pool without stomping each other's quota | After M6 both write the same ledger; combined daily spend ≤ budgets |
| BR6 | The VPS keeps running its external network services untouched | Compliance with deployment notes do-not-touch list; `MemoryMax=350M`; no ufw/port changes |

## B. MCP interface (tools)

- **FR-B1** MUST expose `analyze_large`: quota-aware analysis of a massive text body with
  automatic chunking + consolidation.
- **FR-B2** MUST expose `gemini_generate`: single-shot quota-aware completion (body fits
  limits; no chunking). Same routing/thinking/overflow semantics.
- **FR-B3** MUST expose `quota_status`: live budget table + recommendation for the next
  request (`recommended_model`, `why`).
- **FR-B4** MUST expose `router_logs`: tail of the audit log (filters: model, status,
  source, request_id, limit).
- **FR-B5** SHOULD expose MCP resources: `quota://status` (same table, for clients that
  poll resources).
- **FR-B6** Tool inputs (shared core):

```jsonc
{
  "task": "instructions for the model (what to analyze/produce)",  // required
  "body": "the massive text",                    // required for analyze_large
  "thinking_level": "low|medium|high",           // optional, default: medium
  "output_format": "text|json",                  // optional, default: text
  "max_output_tokens": 8192,                     // optional, per chunk & consolidation
  "model": "gemini-3.8-flash",                   // optional pin; skips fallback chain
  "overflow": "off|lite|lite+gemma",             // optional, default: off (hard stop)
  "wait_for_minute_limits": true,                // optional, default: true — wait out RPM/TPM gates (FR-C13)
  "chunk_tokens": 180000,                        // optional override of chunk_size_tokens
  "overlap_ratio": 0.10,                         // optional override
  "request_id": "caller-side idempotency key",   // optional; replays return cached result
  "result_max_chars": 12000                      // optional cap on returned consolidated text
}
```

- **FR-B7** Tool result MUST be: consolidated text (or JSON when requested) + `meta`:
  `{request_id, models_used[], chunks: {count, tokens_in, overlap}, requests_spent,
  quota_after: {per-model rpd_left}, elapsed_s, cache_hits, progress_trace[]}`.
- **FR-B8** Hard-stop error MUST be structured:
  `{error: "quota_exhausted", per_model: [{model, rpd_left, rpm_wait_s, tpm_wait_s,
  cooldown_until}], reset_eta_pt, hint: "retry after X or pass overflow=lite+gemma"}`.
- **FR-B9** MUST run over two transports from one codebase: stdio and streamable HTTP
  (daemon, bearer-token auth, `127.0.0.1` bind by default).

- **FR-B10** `analyze_file` MUST accept PDFs, images, audio and video by sending the bytes
  to the provider's Files API (option A, owner decision 2026-10-11): one upload, one
  request pinned to the key that owns the file, one measured token count before the
  request so the TPM gate is honest. Exception FR-B14 applies to PDFs over full TPM.
  Text the server can extract (pandoc set, RTF, plain) keeps the chunked pipeline.
  Pre-flight refusals —
  unsupported type, provider document ceilings — spend nothing and say which limit was
  hit. The provider reference is cached in the upload sidecar and reused until it expires
  or its key leaves the configuration.

- FR-B11: binary media (PDF, image, audio, video) is read by the provider at a
  **configurable resolution level** (`ingest.media_resolution`, default `high`):
  the level decides the token price (per-level prices and provider defaults live in ONE
  table — `gemini_router.tariffs.MEDIA_TARIFFS` — and are restated nowhere else) and is
  reported in `meta.file.resolution`; `""` inherits the provider default.
- FR-B12: when the measured media cost misses the router's tariff forecast beyond the
  noise floor, the provider-path result MUST carry `meta.cost.drift`
  (`predicted`/`measured`/`pct`) and the report MUST instruct the agent to tell the
  owner; the miss is recorded as a non-spending ledger row which `doctor` and `quota`
  surface while it is recent. A miss is a signal, never an error.

- FR-B13 `analyze_files(task, upload_ids=[…])` MUST analyze several uploaded files with
  one answer: text documents each keep the chunked pipeline; binaries are uploaded,
  measured with the free count, then packed first-fit-decreasing by measured tokens
  against the serving key's minute budget (uploads are per key, so a pack never crosses
  keys) into as few group requests as fit, and all parts consolidate into one result.
  Only a PDF exceeding the FULL minute TPM is split by page range at its existing
  resolution, re-measured per chunk and consolidated to ONE per-file answer before
  group consolidation; its parts never mix with another file in a provider request
  or first-pass consolidation. A temporary occupied minute window does not trigger
  splitting; other over-budget binaries refuse with measured cost and zero generate
  spend. A single id delegates to `analyze_file`.

- FR-B14: A PDF whose measured tokens + prompt exceed full per-key TPM MUST be
  split into ordered page ranges using bounded-resource qpdf, without changing
  media resolution. Each range MUST be re-measured before generation and subdivided
  if it cannot fit. The provider must not see parts from different source files in
  one request; per-file answers consolidate before joining a multi-file result.
  Report the split with ⚠️, page ranges, and cost; fail explicitly if splitting
  is unavailable or a single page cannot fit.
- FR-B15: Reports MUST list all distinct models used. Each unrequested flash-lite
  or gemma model MUST be marked ⚠️ in the agent-visible report; explicit model pins
  are exempt. Other degraded successes carry ⚠️; failed tool calls are ⛔️ in the
  agent-facing instructions, with a plain-language remedy. Clean success stays clean.

## C. Routing & quota (gemini-router)

- **FR-C1** MUST route through the configured Flash chain (newest first; deprecated
  aliases excluded — see `config.py`), skipping models that are unavailable (`listModels`
  at startup, refreshed every `models_refresh_h`) or cooled down.
- **FR-C2** MUST enforce per **key×model**: RPD (PT-date counter + rolling-24h), RPM
  (sliding 60 s), TPM (sliding 60 s, tokens) — before dispatch (pre-flight) and
  after (accounting from `usageMetadata`).
- **FR-C3** MUST persist every attempt in the SQLite ledger (schema in 03-architecture):
  ts, request_id, source, key_id (masked), model, kind (single|chunk|consolidate|count),
  tokens_in/out, status, http_status, quota_error, latency_ms.
- **FR-C4** MUST handle 429: parse `Retry-After`/quota payload → set cooldown →
  immediately try next model/key → if none: FR-B8 error. 429s with quota exhaustion are
  NOT retried on the same model.
- **FR-C5** MUST retry retryable failures (408/409/429-RPM/5xx/network) with exponential
  backoff + jitter, bounded by `provider_timeout_s`; client 4xx (except 408/429) switch
  model instead of retrying (taxonomy inherited from external-consumer `src/llm.py`).
- **FR-C6** MUST support a key pool (`GEMINI_API_KEYS` / `GEMINI_API_KEY_1..10`);
  selection prefers the key×model pair with the largest remaining budget. The pool is
  generation-agnostic (owner, 2026-10-11): classic `AIza…` (Standard, Cloud Console) and
  newer `AQ.…` (Auth, AI Studio) keys both validate, probe and serve. The transport
  (standard header / query / bearer) is discovered per key from the provider's answer and
  remembered — never assumed from the shape, which Google has changed under us more than
  once (gemini-router SPEC §10). A key the provider blocked as *leaked* is reported as
  itself with the remedy, and leaves the pool like any other dead key.
- **FR-C7** MUST count `countTokens` pre-flight toward TPM planning but NOT toward RPD.
- **FR-C8** Overflow (opt-in per request): tier 1 `gemini-3.5-flash-lite`, tier 2 best
  available `gemma-*` (runtime-resolved, config-overridable). Overflow usage is tagged
  in the ledger and reported in `meta`.
- **FR-C9** MUST be importable with zero MCP/Freebuff dependencies; async API
  (`await router.complete(req, on_event=…)`) + sync facade for legacy callers
  (external-consumer migration).
- **FR-C10** Ledger MUST be multi-process safe (SQLite WAL, `busy_timeout`) so the
  daemon, stdio sessions and cron jobs share one truth.
- **FR-C11** SHOULD cache identical requests: key = hash(model, thinking_level,
  output_format, max_output_tokens, prompt); TTL `cache_ttl_h`; a cache hit spends 0 RPD
  and is marked in `meta.cache_hits`.
- **FR-C12** CLI MUST provide: `gemini-router quota` (table), `models` (live list +
  chain resolution), `calls` (audit tail), `prune` (retention), `doctor` (config/state/
  connectivity check).
- **FR-C13** MUST support a per-request wait policy for **minute-scale** gates
  (RPM/TPM windows, short 429 cooldowns): `wait_for_minute_limits`, default **true**.
  When enabled and the gate's wait ≤ `wait.max_s`, the router waits in place (poll
  `wait.poll_s`), emitting `waiting` events with reason and remaining seconds — frontends
  MUST surface the countdown to the agent (FR-D8, FR-E1..E3). When disabled, or the wait
  exceeds the cap, routing moves to the next candidate immediately; if no candidate is
  available without waiting, the FR-B8 error reports `rpm_wait_s`/`tpm_wait_s` per model
  so the caller can decide. **Daily RPD resets are never waited on** (hours-scale) —
  hard stop / overflow per FR-C8 applies.

## D. Chunking pipeline (gemini-mcp)

- **FR-D1** MUST measure the body with `countTokens`; if ≤ `chunk_threshold_tokens` →
  single-shot path (FR-B2 semantics).
- **FR-D2** MUST split into M chunks ≤ `chunk_size_tokens` with `overlap_ratio` overlap;
  split points prefer paragraph → sentence → line boundaries (never mid-token).
- **FR-D3** Each chunk prompt MUST carry a service marker block:
  `[SYSTEM NOTE — CHUNK N/M] You are analyzing part N of M of one larger document
  (overlap with neighbours is intentional). Analyze ONLY this part per the task.
  Do not summarize the whole document. If a thought continues beyond this part, end
  with "…(continues)".` followed by the user task and the chunk body.
- **FR-D4** MUST pre-flight the whole plan: `(M chunks + ≥1 consolidation) ≤ remaining
  RPD` of usable models and TPM windows; otherwise fail fast with FR-B8 error **before
  spending** (unless cache covers part of the plan).
- **FR-D5** Chunks are processed sequentially with RPM pacing (`min_interval` from
  sliding window), rotating models/keys by budget to spread RPD.
- **FR-D6** Consolidation: original task + all chunk answers (numbered, with overlap-dedup
  instruction) → one coherent result. If the stitched input exceeds the consolidation
  model's limit → **recursive consolidation** (tree-reduce) until it fits.
- **FR-D7** Partial failure: if a chunk permanently fails, continue with the rest and
  mark the gap in the result (`meta.chunks_failed[]`); consolidation is told which parts
  are missing. SHOULD (config `fail_on_chunk_error: true` default) abort instead when the
  caller prefers all-or-nothing.
- **FR-D8** Progress stages (each → FR-E1): `received` → `measured` (tokens, plan M) →
  `budget_checked` (spend/remaining) → `chunk i/M dispatched|done` (model, tokens,
  latency) → `consolidating round r` → `done` (spent, quota_after). A
  `waiting (model, reason, eta_s)` stage may interleave before any dispatch (FR-C13);
  countdown updates are emitted at least every `wait.poll_s`×5.

## E. Progress & observability

- **FR-E1** MUST emit progress via MCP `notifications/progress` when the client supplied
  a `progressToken` (percent + stage message).
- **FR-E2** MUST ALSO emit each stage via MCP logging notifications
  (`notifications/message`, level info) for clients without progress support. (MCP v2
  deprecates the logging capability — SEP-2577; kept for v1-compatible clients, the
  progress `message` channel is primary.)
- **FR-E3** MUST embed the full `progress_trace[]` (stage, ts, detail) in the final
  result meta — the guaranteed floor when a client shows neither (b03).
- **FR-E4** App logs: pretty console (rich) → stderr (journald picks it up) + rotating
  file `logs/app.log` (`RotatingFileHandler`, `log_max_bytes` × `log_backups`).
- **FR-E5** Request logs: SQLite audit (FR-C3) + daily JSONL `logs/requests-YYYY-MM-DD.jsonl`
  (full request/response dumps only when `dump_bodies: true`; default: sizes+hashes).
- **FR-E6** Retention/eviction: JSONL older than `jsonl_keep_days` deleted; audit rows
  beyond `audit_keep_days`/`audit_max_mb` pruned; app.log rotated; on startup and daily.
- **FR-E7** Terminal access: `gemini-mcp logs [-f] [--json]`, `gemini-mcp quota`,
  `gemini-mcp calls [--model m] [--status s] [-n 50]` — work over SSH against the
  server state without stopping the daemon.
- **FR-E8** Secrets: API keys and bearer token MUST never appear in any log/audit/error
  (masked `key_id`, token redacted); bodies are not logged by default.
- **FR-E9** File inbox (M4.5): upload tickets are HMAC-signed, size-bound, TTL-bound and
  spent on FIRST use — the claim is an `O_EXCL` marker that outlives the blob, so a
  replay inside the TTL is refused even after the file is pruned or evicted. Blobs,
  sidecars and spent markers age out at `ingest.keep_hours`, capped by
  `ingest.total_cap_mb` (oldest first), pruned when a ticket is issued, at the start of
  every `analyze_file` and on demand via `gemini-mcp prune`.
- **FR-E10** A running-job ticket MUST say what the job is doing: `live` carries the
  current `phase` (canonical from router events and gateway stages: `generating`,
  `waiting_rpm`/`waiting_tpm`, `chunking`, `consolidating`, …), `eta_s` while a quota
  gate is being waited out (counts down on every poll), `pct`, `model`, `key_id`,
  `attempt` and the latest stage line. Polling surface only — no subscription, no extra
  tool, and a poll never blocks longer than `wait_seconds`.
- **FR-E11** Job progress reported to an agent MUST not move backwards or claim
  100% while a child file still has siblings or group consolidation pending.
  Repeated ticks of one uninterrupted quota wait count as ONE warning episode,
  while the detailed event trace stays intact. Agent-visible errors (including
  direct upload-ticket and REST failures) carry an ⛔️ recovery cue.

## F. Non-functional

- **FR-F1** RAM: daemon ≤ 350 MB (`MemoryMax=350M`), stdio shim target ≤ 120 MB;
  single uvicorn worker; deps: `mcp` (FastMCP), `httpx`, `pydantic`, `rich`, stdlib `sqlite3`.
- **FR-F2** Python 3.12, `uv` install with `--no-cache`; app dirs `/opt/apps/gemini-mcp`;
  shared state `/opt/apps/shared/gemini-router/state.db` (env `GEMINI_ROUTER_STATE`).
- **FR-F3** Startup: stdio shim ready ≤ 3 s (Freebuff session feel); daemon
  `Restart=on-failure`, graceful shutdown, state consistent after kill -9 (WAL).
- **FR-F4** Security: HTTP bearer token (32-byte hex, generated by setup script, stored
  in `.env` chmod 600); constant-time compare; SSH-stdio inherits SSH auth; no new ufw
  rules or bound ports beyond the localhost daemon port (`http_port`, default 8790).
- **FR-F5** Idempotency: same `request_id` within `cache_ttl_h` returns the stored result
  without new spends.
- **FR-F6** Config: one `config.yaml` + `.env` (secrets) per service; all limits,
  chain order, thinking default, chunking, retention, overflow tiers configurable
  without code edits; config validated at startup with clear errors (pydantic).
- **FR-F7** Conventional Commits + SemVer; CI (GitHub Actions, free tier): lint + tests
  on push; router tagged releases consumed by gemini-mcp via git dependency.
- **FR-F8** Timezone: quota day = America/Los_Angeles; all logs store UTC + PT where relevant.

## G. Acceptance scenarios (v1.0 gate)

1. **Fits**: 50k-token body → single-shot on `gemini-3.8-flash`, thinking high, result +
   meta; ledger row; `quota_status` shows 19 RPD left.
2. **Massive**: 900k-token body → plan M≈6 (overlap 10%) → pre-flight passes → 6 chunk
   calls paced under RPM → consolidated (1 call) → compact result; progress visible at
   every stage; meta lists per-chunk model/latency.
3. **Doesn't fit**: plan needs 8 requests, 5 RPD left → immediate structured
   `quota_exhausted` error, zero spends, `hint` suggests overflow or retry time.
4. **Overflow opt-in**: same as (3) with `overflow: "lite+gemma"` → runs on flash-lite,
   tagged in ledger/meta.
5. **Coexistence**: daemon + a parallel `gemini-router` CLI call → both see one budget,
   no double-spend (WAL).
6. **Restart**: kill daemon mid-day → counters intact from ledger.
7. **Ops**: `gemini-mcp quota` / `logs -f` over SSH work while the daemon runs; JSONL
   pruning removes files older than retention.
8. **Wait policy**: model gated by RPM → `wait_for_minute_limits=true`: request waits,
   agent sees countdown ("waiting 42s for gemini-3.8-flash RPM window"), then succeeds;
   `=false`: instantly routed to the next model, or structured error carrying
   `rpm_wait_s`/`tpm_wait_s` when nothing is available without waiting. Zero spends in
   both error paths.
