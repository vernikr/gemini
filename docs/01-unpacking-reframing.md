# 01 · Unpacking & Reframing — Stage 1 artifact

Date: 2026-10-08 · Status: **accepted** (user answered all Stage-1 questions, see §5)

## 1. Original ask (condensed, intent preserved)

Run an MCP server on the personal VPS so a local Freebuff Desktop agent can offload
**massive-body analysis** to Gemini **free tier** (Google AI Studio) without polluting the
agent's own context:

- Top-5 Gemini Flash models, cascading fallback `3.8 → 3.7 → 3.6 → 3.5 → 3`;
  free-tier limits per model: **20 RPD, 5 RPM, 250 000 TPM**.
- A **router with disk logging** of every request, so it is always known which model
  can accept the next request.
- Payloads exceeding the input limit are **split into overlapping chunks**, each analyzed
  separately with a mandatory service marker ("chunk N of M"), then all chunk answers are
  stitched and sent back to Gemini for **consolidation**.
- `thinking_level` default **high**, overridable per request.
- MCP reports **maximally detailed intermediate progress** to the calling agent.
- Staged process with an artifact per stage: (1) unpacking + questions, (2) functional
  requirements, (3) architecture & stack, (4) plan, (5) development.
- The router-like code inside `external-consumer` must be **extracted into a standalone
  project/module reusable by everyone, including externally**.
- Beautiful disk logging, readable from the terminal, with old-log eviction.

## 2. Reframing

This is not "an MCP server". It is a **quota-aware Gemini gateway** with three separable
concerns:

| Concern | Deliverable | Consumer |
|---|---|---|
| Quota truth & model routing | `gemini-router` — Python lib + CLI (+ REST surface via the gateway daemon) | MCP server, `external-consumer` (later), any external project |
| Massive-payload pipeline (chunk → analyze → consolidate) with progress | `gemini-mcp` — MCP server (stdio + streamable HTTP) | Freebuff Desktop agent |
| Shared persistent ledger of every call | SQLite state file on the VPS, owned by the router | all of the above, across processes and restarts |

Key reframe: **the ledger is the product**. With 20 RPD per model the scarce resource is
quota, not compute. Every design decision (pre-flight budget checks, chunk-cost
estimates, caching, overflow policy) serves "never spend a request blind, always know
what is left".

## 3. Verified facts (as of 2026-10-08)

### 3.1 Gemini free tier

| Model ID | Status | Free limits (measured/reported) | Notes |
|---|---|---|---|
| `gemini-3.8-flash` | stable, newest Flash | 5 RPM / 20 RPD / ~250k TPM | 1 048 576 tok input, 65 536 output; `thinking_level`: low/medium/high (medium default; `minimal` errors) |
| `gemini-3.7-flash` | stable | 5 RPM / 20 RPD | fully supported, no shutdown date |
| `gemini-3.6-flash` | stable | 5 RPM / 20 RPD | |
| `gemini-3.5-flash` | stable (legacy label) | 5 RPM / 20 RPD | |
| `gemini-3-flash-preview` | preview | 5 RPM / 20 RPD | 5th chain slot; exact ID resolved at runtime via `listModels` |
| `gemini-3.5-flash-lite` | stable | 15 RPM / 500 RPD | overflow tier 1 (opt-in) |
| `gemma-*` (best available) | stable | generous | overflow tier 2 (opt-in); ID resolved at runtime |

- Google **no longer publishes** free-tier tables; the runtime `429` payload
  (`quotaId`, `quotaValue`, `Retry-After`) is ground truth. Limits are
  **per GCP project, per model** — different keys of one project share the same budget.
- Gemini 3.x migration guidance: use `thinkingConfig.thinking_level` (not
  `thinking_budget`); strip `temperature`/`top_p`/`top_k` for 3.x models. Thinking
  tokens count against token limits — `high` is the most expensive mode; user wants it
  as default anyway, overridable per request.
- Quota day boundary: assume **America/Los_Angeles midnight** plus a rolling-24h safety
  window (exact reset is not documented).
- `countTokens` API is used for pre-flight sizing (not part of generateContent quota).

Sources: aifreeapi.com free-tier survey (2026-10-01), scriptbyai.com limits table
(2026-09-13), aiplug.work measured limits (2026-09-02), lindy.ai 3.8-flash review
(2026-10-01), apidog.com 3.8-flash guide (2026-09-02).

### 3.2 Freebuff Desktop (the calling agent)

- MCP config: `.agents/mcp.json`, searched cwd → parent → home, later files override
  same-named servers; standard `{"mcpServers": …}` shape.
- Supports **stdio** servers (`command`/`args`/`env`, `$NAME` resolved from process env)
  and **http** servers (`type: "http"`, `url`, `headers` — e.g. `Authorization: Bearer $TOKEN`).
- Known bug (CodebuffAI/freebuff#957): config is cached at CLI boot and not reloaded on
  project-picker switch → prefer `~/.agents/mcp.json` for a server-wide wiring.
- Whether Freebuff renders MCP `notifications/progress` is **unverified** → progress is
  designed with a 3-level graceful degradation (see 02-requirements §E).

### 3.3 Target server `YOUR_VPS_HOST` (from owner deployment notes)

- Ubuntu 24.04, 1 vCPU, 1 GB RAM + 2 GB swap, 20 GB SSD; **production external network services on it**
  (external-service/external network service). Do-not-touch: ports 443 & 20724, `unrelated-service`,
  `/usr/local/external-service`, sysctl BBR tuning, `system resolver configuration`, ufw rules for 22/443/20724.
- Background services: `MemoryMax=350M`, Python 3.12 + `uv` (`--no-cache`), apps under
  `/opt/apps/<name>`, journald capped at 100 MB, deploy via rsync script (not git clone).

### 3.4 Existing proto-router: `external-consumer/src/llm.py`

Reusable as-is (move into `gemini-router`):
- priority model list validated against live `listModels`, `_best_available` heuristic;
- retry/backoff taxonomy: `RETRYABLE_STATUS` (408/409/429/5xx), `MODEL_SWITCH_STATUS`
  (400/403/404/405/422), `Retry-After` parsing, per-provider deadline;
- `extract_json` tolerant JSON extractor; `LLMError` shape.

Gaps the new router must close: in-memory-only budgets (lost on restart), per-provider
instead of per-model RPD, **no TPM tracking**, no key pool, no disk audit log, sync
`requests` (not async), no `thinking_level`, no event/progress hooks, no overflow tiers,
no CLI.

**Divergence flag:** owner deployment notes describes `data/llm_state.json` (429 cooldowns,
12 h TTL per key+model) and a 10-key `GEMINI_API_KEYS` pool on the deployed server, but
neither exists in the GitHub copy → the deployed tree is newer than the repo
(→ blocker b02; the new router implements a superset: SQLite ledger instead of JSON).

## 4. Implicit requirements surfaced by unpacking

1. **Shared ledger**: MCP daemon, ad-hoc stdio sessions and (later) `external-consumer` burn
   the *same* per-project quota → all must read/write one persistent store.
2. **Pre-flight budget check**: a chunked job costs M+1 requests; if the plan does not
   fit remaining budget, fail *before* spending anything.
3. **Chunk economics visible to the caller**: estimated cost, spent, remaining — in
   progress messages and in the final result metadata.
4. **Quota-day realism**: rolling windows + PT-date counters + trust-but-verify via 429.
5. **Cache**: identical chunk prompts (retries, re-runs) should not double-spend RPD.
6. **Key hygiene**: keys masked everywhere (`key_id` = hash prefix), never in logs/repo.
7. **Resource envelope**: ≤350 MB RAM per process, logs+state bounded (rotation +
   retention pruning), nothing that touches external network services rules or reserved ports.
8. **Router consumable 3 ways**: Python dependency (git tag), REST (gateway daemon),
   MCP (via `gemini-mcp`) — "usable by everyone including externally".
9. **Transport duality**: same server code runs as stdio (over SSH, no new ports) and as
   HTTP daemon (localhost-bound initially, bearer token ready for later exposure).

## 5. User decisions (Stage-1 Q&A, 2026-10-08)

| # | Question | Decision |
|---|---|---|
| 1 | Freebuff ↔ MCP transport | **Both**: SSH-stdio primary for Freebuff + HTTP daemon on the VPS (localhost bind first) for future/external clients |
| 2 | Gemini keys | **Shared pool** from `/opt/apps/external-consumer/.env` (`GEMINI_API_KEYS`), deploy script reads it; shared quota ledger; `external-consumer` migrates onto the router later |
| 3 | Repo layout | **Two private repos**: `gemini-router` (lib+CLI, installable from anywhere) + `gemini-mcp` (MCP server, depends on router by tag) |
| 4 | All Flash quotas exhausted | **Hard stop by default** (structured error: remaining per model + reset ETA). Caller may opt in per request to overflow: `gemini-3.5-flash-lite` → best available `gemma-*` |

## 6. Risks & unknowns → tracked in `docs/blockers/`

- b01 — no Gemini key in the dev sandbox: unit/integration tests run against mocked
  HTTP; real smoke happens on the VPS at deploy time.
- b02 — `external-consumer` deployed copy diverged from GitHub: read the server copy before
  migrating it (M6).
- b03 — Freebuff progress-notification support unverified: 3-level degradation built in;
  verify at wiring time.
- b04 — volatile model IDs (5th chain slot, gemma): resolve via `listModels` at startup,
  never hardcode; config-overridable.
- b05 — no SSH access from the dev sandbox to the VPS: deployment = user runs one
  provided script from their workstation (same pattern as `external-consumer/scripts/run_remote.sh`).
- b06 — HTTP transport is plaintext until exposed via tunnel: daemon binds `127.0.0.1`;
  remote use only through SSH tunnel or a future cloudflared TLS tunnel.

## 7. Non-goals (v1)

- Paid tiers / billing; Vertex AI.
- Binary multimodal payloads (images, PDF bytes) — text bodies only; parts-based
  extension point kept in the request schema.
- Streaming of the model answer (progress notifications ≠ token streaming).
- Multi-user auth / per-user quotas (single bearer token, single owner).
- Web dashboard (CLI covers observability).
- Rewriting `external-consumer` business logic (migration = swap the LLM client, keep pipeline).
