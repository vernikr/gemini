# 03 · Architecture & Stack — Stage 3 artifact

Date: 2026-10-08 · Inputs: `01`, `02` · Rule: one codebase, two transports, one ledger.

## 1. System view

```
Freebuff Desktop (macOS/Windows)                     VPS YOUR_VPS_HOST (Ubuntu 24.04, 1GB RAM)
┌──────────────────────────────┐                    ┌───────────────────────────────────────────┐
│ ~/.agents/mcp.json           │                    │  systemd: gemini-mcp.service (MemoryMax=350M)│
│  A) stdio: ssh root@vps ────┼──── SSH (22) ─────►│  ┌─────────────────────────────────────┐  │
│  B) http: 127.0.0.1:8790 ────┼── ssh -L tunnel ──►│  │ gemini daemon (FastMCP/uvicorn) │  │
│     (Bearer $GEMINI_MCP_TOKEN)│   (or future CF)   │  │  MCP: /mcp (streamable HTTP)        │  │
└──────────────────────────────┘                    │  │  REST: /v1/quota /v1/complete       │  │
                                                    │  │  pipeline: chunker→analyzer→consol. │  │
   ssh-launched stdio session (A) runs the SAME     │  └──────────────┬──────────────────────┘  │
   package on the VPS, standalone:                  │   stdio session │ (ephemeral, per agent)  │
   python -m gemini_mcp stdio                       │  ┌──────────────▼──────────────────────┐  │
                                                    │  │ gemini-router (library, async)      │  │
   external-consumer (cron, M6) ─────────────────────────►│  │  strategy · keypool · budgets · 429 │  │
                                                    │  │  cache · events · CLI               │  │
   external projects ── uv add git+…gemini-router ──┤  └──────────────┬──────────────────────┘  │
                                                    │        SQLite WAL│ /opt/apps/shared/       │
                                                    │  ┌──────────────▼──────────────────────┐  │
                                                    │  │ gemini-router/state.db              │  │
                                                    │  │  calls · cooldowns · cache · meta   │  │
                                                    │  └─────────────────────────────────────┘  │
                                                    │  logs/: app.log (rotating) · requests-*.jsonl│
                                                    └───────────────────┬───────────────────────┘
                                                                        │ httpx (async, HTTP/2)
                                                                        ▼
                                          generativelanguage.googleapis.com/v1beta
                                          models:generateContent · models:countTokens · models:list
```

Trust/encryption: path A is SSH end-to-end. Path B binds `127.0.0.1` — remote use only
via `ssh -L 8790:127.0.0.1:8790` (documented) or a future cloudflared TLS tunnel (b06).
Bearer token guards B (and localhost REST) regardless.

## 2. Repos & module layout

### 2.1 `gemini-router` — the reusable core (no MCP deps)

```
gemini-router/
├── pyproject.toml            # name=gemini-router, SemVer tags v*; console: gemini-router
├── src/gemini_router/
│   ├── __init__.py           # public API: Router, RouterRequest, RouterResult, RouterEvent, RouterError
│   ├── config.py             # pydantic-settings: env + yaml; limits, chain, overflow tiers
│   ├── ledger.py             # SQLite WAL store: calls/cooldowns/cache/meta; PT-day & rolling windows
│   ├── gemini.py             # httpx client: generateContent, countTokens, listModels; 3.x params
│   ├── routing.py            # chain walk, key×model budget pick, retry/backoff, 429 taxonomy
│   ├── events.py             # RouterEvent stream (dataclass) → on_event callback (progress source)
│   ├── errors.py             # QuotaExhaustedError(payload per FR-B8), RouterError
│   └── cli.py                # quota | models | calls | prune | doctor  (rich tables)
├── tests/                    # pytest + respx (mocked HTTP); no network in CI
└── docs/SPEC.md              # API contract (stage-2/3 slice for this repo)
```

Public API (async):

```python
router = Router.from_env()                     # or Router(Config(...))
result = await router.complete(
    RouterRequest(prompt=…, system=…, thinking_level="high", output_format="json",
                  max_output_tokens=8192, kind="chunk", request_id=…, overflow="off",
                  wait=True, source="mcp"),    # wait: minute-scale gates (FR-C13)
    on_event=lambda e: …)                      # RouterEvent(stage, pct, model, wait_s, detail)
# RouterResult(text, model, key_id, tokens_in, tokens_out, latency_s, cached, events[], quota_after)
sync_facade: router.complete_sync(…)           # for external-consumer migration (M6)
```

Inherited from `external-consumer/src/llm.py` (extract, don't rewrite): status taxonomy
(`RETRYABLE_STATUS`, `MODEL_SWITCH_STATUS`), `Retry-After` parsing, `extract_json`,
`listModels` validation + `_best_available`, backoff with jitter and provider deadline.

### 2.2 `gemini` — gateway & pipeline

```
gemini/
├── pyproject.toml            # deps: mcp[cli], gemini-router @ git tag, rich; console: gemini
├── config.yaml               # all tunables (limits, chain, chunking, retention, thinking default)
├── .env.example              # GEMINI_API_KEYS, GEMINI_MCP_TOKEN, GEMINI_ROUTER_STATE, …
├── src/gemini_mcp/
│   ├── server.py             # MCPServer (SDK v2), resources, REST routes, bearer middleware
│   ├── tools.py              # analyze_large, gemini_generate, quota_status, router_logs
│   ├── pipeline.py           # measure → plan → budget pre-flight → chunk loop → consolidate
│   ├── chunker.py            # token-aware splitter (paragraph>sentence>line), overlap windows
│   ├── prompts.py            # chunk service marker, consolidation & recursive-consolidation templates
│   ├── progress.py           # RouterEvent+pipeline stages → report_progress / ctx.info / trace
│   ├── logging_setup.py      # rich console + RotatingFileHandler + JSONL audit + redaction + retention
│   ├── cli.py                # gemini serve|stdio|quota|calls|logs|prune|doctor|tts
│   ├── tts_cli.py            # local MP3 CLI; direct keys or short-lived VPS proxy tunnel
│   ├── tts_client.py         # SSH token acquisition + loopback-only tunnel lifecycle
│   ├── fleet_profile.py     # mode-0600 host/user profile (no credentials)
│   └── fleet_profile.py      # mode-0600 host/user profile (no credentials)
├── scripts/                  # setup_server.sh (idempotent), run_remote.sh (rsync deploy), gen_token.sh
├── tests/                    # pipeline & chunker units (fake token counter), MCP e2e via in-memory client
└── docs/                     # this stage set + blockers + freebuff-wiring.md
```

## 3. Ledger schema (SQLite, WAL, `busy_timeout=5000`)

```sql
-- The executable definition is gemini-router `src/gemini_router/ledger.py` SCHEMA.
CREATE TABLE calls(              -- one row per Gemini attempt (incl. failures)
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, ts_iso TEXT NOT NULL,  -- unix epoch + UTC ISO
  request_id TEXT, source TEXT NOT NULL DEFAULT 'cli',  -- mcp|cli|rest|tldr|count|smoke
  kind TEXT NOT NULL DEFAULT 'single',                  -- single|chunk|consolidate|count
  key_id TEXT NOT NULL, model TEXT NOT NULL,            -- key_id = sha256(key)[:8]
  status TEXT NOT NULL,   -- planned|ok|quota|http4xx|http5xx|network|blocked|skipped
  http_status INTEGER, tokens_in INTEGER, tokens_out INTEGER,
  quota_error TEXT, retry_after_s REAL, latency_ms INTEGER,
  overflow_tier INTEGER NOT NULL DEFAULT 0);
CREATE INDEX idx_calls_model_ts ON calls(model, ts);
CREATE INDEX idx_calls_key_model_ts ON calls(key_id, model, ts);
CREATE INDEX idx_calls_req ON calls(request_id);

CREATE TABLE cooldowns(key_id TEXT NOT NULL, model TEXT NOT NULL, until_ts REAL NOT NULL,
  reason TEXT, PRIMARY KEY(key_id, model));
CREATE TABLE cache(hash TEXT PRIMARY KEY, request_id TEXT, model TEXT NOT NULL,
  ts REAL NOT NULL, response TEXT NOT NULL, tokens_in INTEGER, tokens_out INTEGER);
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);       -- schema_version
```

Budget queries (single source of truth, no in-memory counters that can drift):
- RPD left = `rpd − count(calls WHERE model,key_id, status IN
  ('ok','quota','http5xx','blocked') AND ts ≥ pt_midnight)`; rolling-24h count as a
  second gate. Conservative rule: 429s count as spent (Google bills the attempt); pure
  client-validation 4xx and network errors do not. `kind='count'` never counts.
- RPM/TPM windows = same table, `ts ≥ now−60s` (TPM uses `tokens_in`; pre-flight adds
  the planned request's countTokens estimate as a pending row with status='planned',
  upgraded to real status after the call — prevents burst races between processes).

## 4. Key sequences

**Single-shot** (`gemini_generate` / body fits): measure (countTokens) → pick key×model
(max budget, chain order, not cooled down) → pre-flight RPD/RPM/TPM → **wait gate
(FR-C13): if blocked by RPM/TPM/short cooldown and `wait` enabled and eta ≤ `wait.max_s`
→ emit `waiting` countdown events, poll `wait.poll_s`; otherwise next candidate** →
cache probe → generate (thinking_level) → account from `usageMetadata` → ledger row →
result+meta.
On 429/5xx: taxonomy → cooldown/next model (FR-C4/5). Exhausted → FR-B8 error or
overflow tiers if opted in.

**Massive body** (`analyze_large`):
1. `measure`: countTokens(body) → tokens_total.
2. `plan`: M = ceil over `chunk_size_tokens` with overlap; cost = M + consolidation
   rounds (≥1); report plan via progress.
3. `budget pre-flight` (FR-D4): simulate spend across chain (spread over models with
   most RPD left); if impossible → fail fast, zero spend.
4. `chunk loop` i=1..M: build marked prompt (FR-D3) → router.complete(kind=chunk,
   pacing min_interval) → progress `chunk i/M done (model, tokens, latency)`; cache hits
   skip spends. Failure policy FR-D7.
5. `consolidate`: task + numbered chunk answers + overlap-dedup instruction →
   router.complete(kind=consolidate); if stitched input > limit → recursive tree-reduce
   (round r+1 consolidates groups of `consolidation_fanin` summaries).
6. `assemble`: result as the model returned it + meta (models_used, spent, quota_after,
   progress_trace, chunks_failed, result_chars). No length cap — see BR1.

**Progress mapping** (FR-E1..3): every RouterEvent/pipeline stage →
`ctx.report_progress(pct, message)` (when progressToken present) AND `ctx.info(message)`
AND append to `progress_trace`. Percent budget: measure 5%, plan 5%, chunks 80% (i/M),
consolidation 10% (per round). Note: MCP v2 deprecates the logging capability
(SEP-2577) — `ctx.info` is still emitted for v1-compatible clients; the progress
message channel is primary, the embedded trace is the floor. Multi-file calls
map each child pipeline's 0–100 into a bounded span of the parent's progress;
stages hold a monotone high-water mark. A child's `done` means `file_complete`,
not whole-job completion. The report groups consecutive ticks of the same
key/model/reason quota wait as one `rough_edges` episode, leaving the full trace
unchanged for diagnostics.

**Live status of a running job** (FR-E10): async tools (`wait_seconds` set) run as a
background job keyed by `request_id`; `_jobbed` hands the job's emitter a plain dict
shared with JobManager. Every stage/router event mirrors its latest state into it —
canonical phase (`generating`, `waiting_rpm`/`waiting_tpm`, `chunking`,
`consolidating`, …), pct, latest stage line, model, key_id and, while a gate is being
waited out, `wait_until`. Every running ticket returned by `analyze_result` carries it
as `live`, with `eta_s` recomputed at read time: a poller sees the countdown without
blocking and without subscribing to progress notifications.

## 5. Logging & eviction ("beautiful, terminal-accessible, self-cleaning")

| Stream | Sink | Format | Eviction |
|---|---|---|---|
| App log | stderr → journald; `logs/app.log` | rich (color, aligned, dim ts) when TTY; plain otherwise | RotatingFileHandler `log_max_bytes`(10MB)×`log_backups`(7); journald cap already 100M |
| Audit | `state.db calls/cooldowns` | SQL | prune > `audit_keep_days`(30) or table > `audit_max_mb`(50) |
| Request dumps | `logs/requests-YYYY-MM-DD.jsonl` | one JSON/line: request_id, tool, params hash, sizes, stages, spends, errors; bodies only if `dump_bodies` | delete files > `jsonl_keep_days`(14); startup + daily task |
| Redaction | logging filter | API keys → `key_id`, bearer → `***`, emails/long tokens masked | always on |

Terminal UX (over SSH, no daemon stop): `gemini quota` (rich table: model × key,
RPD left bar, RPM/TPM window, cooldown ETA, recommended next), `gemini logs -f`
(tail app.log; `--json` for JSONL), `gemini calls -n 50 --status quota`,
`gemini doctor` (config, state path writable, key validity via listModels, clock/PT
offset, RAM). Same tables exposed by MCP `quota_status`/`router_logs` and REST `/v1/quota`.

## 6. Deployment topology (VPS)

```
/opt/apps/gemini-mcp/          # rsync'd from workstation (scripts/run_remote.sh pattern)
├── .env                       # chmod 600: GEMINI_API_KEYS (copied from external-consumer .env by setup), GEMINI_MCP_TOKEN, GEMINI_ROUTER_STATE
├── .venv/                     # uv venv + uv pip install --no-cache (router from git tag)
├── config.yaml · src/ · scripts/ · prompts inline
└── logs/                      # app.log*, requests-*.jsonl
/opt/apps/shared/gemini-router/state.db   # GEMINI_ROUTER_STATE (dir chmod 770, db 660)
/etc/systemd/system/gemini-mcp.service    # ExecStart=~/.local/bin/gemini serve
                                          # MemoryMax=350M, Restart=on-failure, EnvironmentFile=.env
```

- Stdio sessions (path A): `ssh root@YOUR_VPS_HOST '~/.local/bin/gemini stdio'`
  — no unit needed; optional sshd slice drop-in caps session RAM (documented, off by default).
- Ports: only `127.0.0.1:8790` (daemon). **No ufw changes** (loopback isn't filtered);
  443/20724/22 untouched; external-service untouched.
- Setup: `scripts/setup_server.sh` — idempotent: dirs, venv, deps, token gen (if absent),
  key import from `/opt/apps/external-consumer/.env` (if `GEMINI_API_KEYS` unset), unit install,
  `daemon-reload`, enable+start, doctor run. Never edits unrelated-service/ufw/sysctl.

Client wiring (both transports, quirks and verification prompts): `freebuff-wiring.md`.

## 7. Stack & rationale

| Choice | Why | Rejected alternative |
|---|---|---|
| Python 3.12 + uv | server standard, external-consumer compat, `--no-cache` disk discipline | TS/Bun (ecosystem split on this VPS), Go (faster cold start but team/repo context is Python) |
| Official `mcp` SDK **v2** (`MCPServer`; FastMCP was renamed in 2.x) | one codebase → stdio + streamable HTTP; progress notifications with message; `custom_route` for REST; `call_tool` for tests | hand-rolled JSON-RPC, pinning `mcp<2` |
| `httpx` (async, HTTP/2) | concurrency for pacing + countTokens pre-flight; fine-grained timeouts | `requests` (sync; inherited code wraps it in facade) |
| SQLite (stdlib, WAL) | zero-dep durable multi-process ledger; 20 GB disk, tiny write rate | JSON+filelock (fragile), Redis/Postgres (RAM/ops cost on 1 GB box) |
| `pydantic` v2 | config validation, result schemas (already an mcp dep) | dataclasses-only (weaker config DX) |
| `rich` | "beautiful" console/CLI/logs requirement, small | plain logging (fails UX req) |
| uvicorn single worker | RAM cap; load ≈ a few req/min | gunicorn multi-worker (RAM, no benefit) |
| GitHub Actions free CI | lint+test per push, tags → releases | none |

Process/RAM budget: daemon ≈ 120–180 MB (FastMCP+httpx+rich), stdio shim ≈ 80–120 MB
(ephemeral), external-consumer run ≤ 350 MB (existing) — worst-case sum < 700 MB with swap
headroom; `MemoryMax=350M` per unit guarantees external-service is never threatened.

## 8. Configuration surface (config.yaml — defaults decided here, values live there)

```yaml
router:
  chain: [gemini-3.8-flash, gemini-3.6-flash, gemini-3-flash-preview]   # 3.5/3.7 are deprecated aliases
  overflow_tiers: [gemini-3.5-flash-lite, gemma-auto]     # used only when request overflow != off
  limits_per_key_model: {rpd: 20, rpm: 5, tpm: 250000}
  limits_overrides: {gemini-3.5-flash-lite: {rpd: 500, rpm: 15}}
  quota_day_tz: America/Los_Angeles
  retry: {max: 3, backoff_base_s: 5, provider_timeout_s: 120, request_delay_s: 2}
  wait: {enabled_default: true, max_s: 65, poll_s: 2}    # minute-scale gates only (FR-C13)
  window_s: 60                                            # sliding RPM/TPM window length
  models_refresh_h: 12
  cache_ttl_h: 24
pipeline:
  chunk_threshold_tokens: 200000      # > this → chunking path
  chunk_size_tokens: 180000           # TPM headroom under 250k
  overlap_ratio: 0.10
  consolidation_fanin: 6              # recursive reduce group size
  fail_on_chunk_error: true
thinking: {default: medium}
server: {http_port: 8790, http_bind: 127.0.0.1, auth: bearer}
logging: {log_max_bytes: 10485760, log_backups: 7, jsonl_keep_days: 14,
          audit_keep_days: 30, audit_max_mb: 50, dump_bodies: false}
state: {path_env: GEMINI_ROUTER_STATE}   # default /opt/apps/shared/gemini-router/state.db
```

Env (`.env`): `GEMINI_API_KEYS` (comma) or `GEMINI_API_KEY_1..10`, `GEMINI_MCP_TOKEN`,
`GEMINI_ROUTER_STATE`, `GEMINI_ROUTER_CONFIG`.

## 9. Design decisions log (one-liners)

- D1: Ledger = SQL queries over `calls`, not counters → crash-proof, multi-process, auditable.
- D2: 'planned' pending rows for TPM pre-flight → closes the countTokens→send race.
- D3: Conservative RPD accounting (429 counts as spent) → never over-promise budget.
- D4: Same package serves stdio & HTTP → no shim drift; stdio works even if daemon is down.
- D5: REST routes live on the daemon (Starlette mount), not a second service → 1 unit, 1 RAM budget.
- D6: Overflow is per-request opt-in (user decision), never automatic → predictable spend.
- D7: thinking default medium is a service config, per-request override wins; gemma ignores it (logged).
- D8: Chunk service marker is a prompt-template file, editable without code (prompts inline in `prompts.py` as constants + optional `prompts/` dir override).
- D9: Waiting covers minute-scale gates only (RPM/TPM/short cooldowns, capped at `wait.max_s`); waiting hours for a daily reset inside one tool call is impractical — the caller gets ETAs (progress + FR-B8 error) and decides: retry later or opt into overflow.
- D10: File ingest = server-side helper (§10), self-hosted presigned pattern on the existing daemon (HMAC one-time URL) + scp channel — no object storage, no local shim on the client.
- D12: Non-text files go to the provider (owner decision 2026-10-11, option A): local
  extraction mangles exactly the pages that matter, and a page mixes text with images.
  Everything the server CAN extract keeps the text pipeline — that is where chunking,
  caching and cheap tokens live. Provider facts the design leans on: 2 GB per file,
  20 GB per project, 48 h retention, no download back, and a file visible only to the
  project of the key that uploaded it.
- D11: Keep the client maximally light — all file processing (starting with RTF→text extraction) happens on the server; the client's job is one `curl`/`scp` and one tool call.

## 10. File ingest — "hand over a file, agent context stays clean" (D10)

Problem: files live on the client machine; the server cannot read client paths, and
an agent that reads a file to paste it into `body` pollutes the very context this
project protects. Decision D10 (2026-10-08, user): server-side helper (NOT a local
shim), presigned-URL pattern. Classic presigned URLs need object storage; we
self-host the same primitive on the existing daemon — zero new infrastructure.

Flow:

1. MCP tool `file_upload_ticket(filename, size_bytes)` reserves `upload_id` (uuid4)
   and returns `{upload_id, upload_url, scp_path, expires_in_s: 900}` where
   `upload_url = http://127.0.0.1:8790/v1/upload/<id>?exp=<unix>&sig=<hmac>`,
   `sig = HMAC-SHA256(GEMINI_MCP_TOKEN, "<id>|<exp>|<size>")` (compare_digest),
   `scp_path = root@<vps>:/opt/apps/gemini-mcp/inbox/<id>`.
2. Agent uploads the bytes — either channel (Freebuff has a shell):
   - HTTP `PUT` to `upload_url` through the SSH tunnel (`ssh -N -L 8790:127.0.0.1:8790
     root@<vps>`): the signature IS the auth (route exempt from bearer), 15-min expiry,
     size cap, ONE use. The route claims `inbox/<id>.consumed` with `O_EXCL` before the
     body is buffered (a spent 20 MB PUT must not enter the memory budget), and the
     marker outlives the blob, so a replay inside the TTL is refused even once the file
     is pruned or evicted. The blob-exists check stays as the second line — it is what
     names the useful next call (`already_uploaded` → `analyze_file`);
   - `scp <file> <scp_path>`: no tunnel needed, the SSH key is the auth; nothing claims
     the ticket, and the blob itself blocks a later PUT of it.
   Server writes `inbox/<id>.part` → atomic rename → `chmod 600`.
3. MCP tool `analyze_file(task, upload_id, …same knobs as analyze_large…)` routes the
   blob by what it is (FR-B10):
   - **text** the server can extract (pandoc set, RTF, plain) → the existing pipeline
     chunks/consolidates it, with caching and progress unchanged;
   - **PDF/image/audio/video** → the provider's Files API: the blob stays on disk and is
     streamed to the provider by the router library, the token cost is measured
     (`count_file_tokens`, free per FR-C7) and ONE request follows. The first upload
     is key-local; PDF requests follow text routing and re-upload the local blob if
     another key is chosen. Image/audio/video-only calls retain key pinning.
     Exception: a PDF whose measured request exceeds FULL TPM is split into page
     ranges (FR-B14).
   - anything else fails fast with `binary_file_needs_conversion` (ZERO spend); a PDF over
     the pre-flight ceilings fails with `pdf_too_large`/`pdf_too_many_pages`, also before
     any upload. `pdfinfo` (poppler-utils) supplies the page count; it never reads content.
   `meta.file = {filename, bytes, chars|mime, encoding, extractor, tokens?, provider?}`
   where the provider block carries `{name, uri, expires_at, key_id}`.
   The reference is cached in the upload sidecar and reused while unexpired and while its
   key is still configured — the blob cannot change for a given `upload_id`, so re-analysis
   costs one request, not one upload. When a key switch succeeds, the sidecar is updated
   to the new serving reference. Cache keys mix in the blob's content hash, never the
   provider URI (a URI changes on every re-upload).
   Cost drift (FR-B12): the router's tariff table (`gemini_router.tariffs.MEDIA_TARIFFS`,
   the single copy of the prices) predicts the measured cost before the call; a miss beyond
   the noise floor lands as `meta.cost.drift` plus a one-line instruction to tell the owner
   and a non-spending ledger row that `doctor`/`quota` surface. A pdf with unknown page
   count gets no prediction, and a miss never fails the request.

   Several files, one answer (FR-B13, `analyze_files`): each text document keeps its own
   pipeline run; each binary is uploaded (or its reference reused) and token-measured for
   free, then packs are built first-fit-decreasing against the serving key's remaining
   FULL minute budget minus a prompt overhead (a temporarily occupied minute is
   waited out, not treated as a reason to split). A pack never mixes provider files
   from different keys: for packs containing PDFs, failover uploads the entire
   local-source pack to the new key first. Non-PDF packs keep prior key pinning.
   Every pack is one request (`kind='chunk'`). A PDF
   over FULL TPM instead goes through qpdf: page ranges are uploaded and re-measured
   at the same resolution, split further if a measured range does not fit, then each
   range gets a separate request containing that PDF part only. Ranges consolidate to
   ONE per-file answer before any group merge; they never mix with other source files.
   Other over-budget binaries refuse with their measured cost. Scratch files are
   deleted after the job; group answers and per-file answers use recursive fan-in.
4. Retention (FR-E9): blobs, sidecars and spent-ticket markers age out at `keep_hours`,
   the total is capped by `total_cap_mb` (oldest first); prune runs on ticket issuance,
   at the start of every `analyze_file` and on demand via `gemini prune`, so an
   inbox nobody asks tickets from still empties. A blob that has been analyzed stays
   until `keep_hours` — re-analyzing the same `upload_id` is a supported second call,
   and the spent ticket cannot be replayed regardless. The per-file cap is a memory
   guard, not a policy — the pipeline holds
   the extracted text in RAM while chunking, so it is sized against `MemoryMax`
   rather than against what the provider would accept. `ingest.allow_oversize` lets
   the owner lift it; while it is on, an agent may issue an oversized ticket with
   `override_size_limit`, which the MCP instructions require it to do only after the
   end user accepts the risk.

"Server decides how to split": text-like payload → semantic chunker (§4); any file
is accepted for upload, the split/convert decision happens server-side at step 3.
RTF: a minimal SERVER-SIDE RTF→text extractor ships with M4.5 (D11: keep the client
maximally light — zero client-side conversion steps): strip control words, decode
`\'XX` (Cyrillic codepages) and `\uNNNN` escapes, `\par`/`\line` → newlines.
Compressed/binary (docx, pdf, zip) → fail-fast `binary_file_needs_conversion`
until server-side converters land (deferred).
