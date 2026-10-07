# 04 · Plan — Stage 4 artifact

Date: 2026-10-08 · Inputs: `01`–`03` · Convention: every milestone = Conventional
Commits + push to origin + artifact listed below. Chat report after each: PM-style, Russian.

## Milestones

### M0 · Scaffolding (both repos)
- pyproject (uv), src layouts per 03 §2, `.gitignore`, CI workflow (ruff + pytest, free runners),
  AGENTS.md per repo, config schemas (pydantic) with defaults from 03 §8, `.env.example`.
- **Artifact**: green CI badges on empty-ish skeletons; `uv run pytest` passes trivially.
- Acceptance: `uv sync` works from clean clone on py3.12; `gemini-router --help` /
  `gemini-mcp --help` run.

### M1 · gemini-router core → tag v0.1.0
- ledger.py (schema 03 §3, WAL, PT-day + rolling windows, 'planned' rows, prune),
  gemini.py (generateContent/countTokens/listModels, thinking_level, 3.x param rules),
  routing.py (extracted tldr taxonomy + key×model selection + cooldowns + overflow tiers),
  events.py, errors.py (FR-B8 payload), cli.py (quota/models/calls/prune/doctor), sync facade.
- Tests: respx-mocked HTTP (b01): chain fallback on 404, 429→cooldown→next model,
  RPD/RPM/TPM gating incl. cross-process (two Router instances, one file), cache hit,
  overflow opt-in, wait policy (wait-vs-skip on RPM gate, countdown events, cap),
  PT-day rollover, extract_json suite (ported from tldr tests).
- **Artifact**: v0.1.0 tag; SPEC.md updated to as-built; CLI sample outputs in docs.
- Acceptance: FR-C1..C12 unit-covered; `gemini-router doctor` explains every failure mode.

### M2 · gemini-mcp server skeleton (daemon + stdio) → gemini-mcp v0.1.0
- server.py (FastMCP, both transports, bearer auth on HTTP), tools.py (all four tools),
  logging_setup.py (rich + rotation + JSONL + redaction), retention.py, rest.py
  (/v1/quota, /v1/complete, /healthz), progress.py, cli.py (serve/stdio/logs/quota/calls/doctor).
- Tests: in-memory MCP client (SDK) over stdio app: tool schemas, quota_status table,
  progress notifications with/without progressToken (FR-E1..E3), auth 401, logs redaction.
- **Artifact**: local e2e transcript (mocked router) committed under `tests/fixtures/`.
- Acceptance: same behavior via stdio and HTTP; daemon down ⇒ stdio still works (D4).

### M3 · Chunking pipeline → v0.2.0
- chunker.py (token-aware, boundary hierarchy, overlap windows), prompts.py (chunk marker,
  consolidation, recursive reduce), pipeline.py (measure→plan→pre-flight→loop→consolidate→
  assemble, FR-D1..D8), request_id idempotency, partial-failure policy.
- Tests: fake token counter (deterministic splits, overlap correctness, never mid-line),
  plan/budget pre-flight matrix (fits / doesn't fit / cache-assisted), recursive
  consolidation depth, progress stage sequence assertions, meta schema.
- **Artifact**: pipeline test report + one synthetic 900k-token run (mocked) transcript.
- Acceptance: scenarios G1–G4 from 02 pass on mocks; zero-spend guarantee on fail-fast (G3).

### M4 · Deployment on YOUR_VPS_HOST → v0.3.0
- scripts/: setup_server.sh (idempotent, external network services-safe per 03 §6), run_remote.sh (rsync deploy),
  gen_token.sh; systemd unit; docs/freebuff-wiring.md (A/B snippets + tunnel recipe +
  bug #957 workaround); real-key smoke test script (`scripts/smoke.sh`: listModels →
  countTokens → 1 tiny generate on 3.8 → quota table) — first real API spend: 1–2 RPD.
- **Executed by the user from their workstation** (b05): `bash scripts/run_remote.sh --setup`
  then `--deploy`; I provide exact commands + expected outputs; smoke runs on the server.
- **Artifact**: doctor output + quota table from the live server; Freebuff session check
  (user sends one `analyze_large` from Freebuff; we inspect ledger/JSONL together).
- Acceptance: BR6 compliance checklist signed off (ports/ufw/external-service untouched, MemoryMax
  observed via systemd-cgtop, disk added < 200 MB); scenarios G5–G8 live (wait countdown
  visibility in Freebuff recorded per b03 findings).

### M5 · Hardening & DX → both repos v1.0.0
- Overflow tiers live-tested (1 lite call), retention eviction live-tested (fake old
  files), restart-consistency drill, README as-built (RU quickstart + EN reference),
  release notes, AGENTS.md polish, blockers archived.
- **Artifact**: v1.0.0 tags; one-page ops card (start/stop/logs/quota/rollback/backup state.db).

### M6 · external-consumer migration (follow-up, separate PR in that repo)
- Read deployed copy first (b02: server has llm_state.json + key pool absent from GitHub);
  replace `src/llm.py` internals with `gemini-router` sync facade; point
  `GEMINI_ROUTER_STATE` at the shared db; keep its config.yaml knobs mapped to router config.
- **Artifact**: migration PR + one digest run showing ledger rows with source='tldr'.
- Acceptance: BR5 — combined spend visible in one `quota` table; digest output unchanged.

## Sequencing & dependencies

M0 → M1 → M2 → M3 can proceed fully in the dev sandbox (mocked, b01).
M4 needs the user's workstation (SSH) — the only synchronous user step; everything after
is verifiable from server outputs the user pastes back (or from my sandbox once an SSH
key is provided — optional accelerator).
M5 after M4 evidence. M6 anytime after M5 (independent repo, no rush).

## Verification matrix (requirement → milestone)

FR-B*: M2/M3 · FR-C*: M1 (C13 wait policy: M1) · FR-D*: M3 · FR-E*: M2 (E4-E8 M2/M4) ·
FR-F*: M0/M4 · BR1-4: M4/M5 · BR5: M6 · BR6: M4 checklist · G1-G8: M1/M3 (mocks) + M4 (live).

## Risks → responses

- Real free-tier limits differ from measured tables → config-overridable + 429-driven
  truth (M1); doctor reports divergence.
- Freebuff ignores progress notifications (b03) → FR-E3 trace in result meta; verify M4.
- SSH stdio latency/flakiness from Freebuff → path B (tunnel+HTTP) as documented fallback.
- VPS RAM pressure with daemon + digest overlap → MemoryMax per unit; digest timer and
  agent usage rarely coincide; ledger pacing prevents bursts.
- Key pool import finds divergent server .env (b02) → setup script reads the deployed
  .env directly, never the GitHub copy.

## Definition of Done (v1.0.0)

G1–G8 pass live; both repos tagged, CI green; README/ops card current; blockers b01–b06
resolved or archived with rationale; user's Freebuff performs one real massive-body
analysis end-to-end with visible progress and a compact result.
