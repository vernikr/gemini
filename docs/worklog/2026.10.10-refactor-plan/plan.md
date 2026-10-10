# Refactoring plan — `gemini-router` + `gemini-mcp`

Date: 2026-10-10 · Scope: `vernikr/gemini-router` @ `5261759` (v0.1.1), `vernikr/gemini-mcp` @ `3672b94` (v0.12.0)
Canonical target: `gemini-mcp/docs/worklog/2026.10.10-refactor-plan/plan.md`

Every number and every "verified" claim below was measured in a sandbox against these two
commits. Cost: **S** ≤ 2 h · **M** ≈ 0.5–1 day · **L** > 1 day. Risk = chance the change
breaks something that works today.

---

## 0. Baseline (measured, not assumed)

| | gemini-router | gemini-mcp |
|---|---|---|
| `src` LOC | 1 233 | 3 038 |
| `tests` LOC / count | 585 / **45** | 1 700 / **105** |
| `docs` + README + AGENTS LOC | 126 + 48 + 25 | **1 808** + 150 + 30 |
| scripts + npm shim LOC | 0 | 588 |
| `ruff check` | pass | pass |
| `pytest -q` | **45 passed in 1.95 s** | **105 passed in 2.01 s** |
| CI wall time (last runs) | 13–20 s | 14–19 s (release: 3–4 min) |

Two conclusions drive the whole plan:

1. **Tests and CI are already fast and green.** There is almost nothing to win in raw
   build time. The CI work that remains is about *redundant* jobs and *honest* signals,
   not speed.
2. **gemini-mcp carries 1 988 lines of docs against 3 038 lines of code, and a
   measurable share of those docs are wrong.** For an AI agent this is worse than no
   docs: it burns context and then acts on stale instructions. This is the single
   biggest lever on agent development speed.

Priority order below follows the requested ranking. Where one task serves two priorities
it is listed once, at the higher one, with a cross-reference.

---

## 0.1 Standing rule added by the owner: no guarded counters in docs

Every number, version, date or "N green" claim written into a document is a fact that
goes stale silently and then needs a check — or a human — to catch it. Remove the
artifact instead of adding the check. This is what produced the §1.1 table: none of
those stale facts was anybody's fault, they were simply unmaintainable by construction.

**Remove** (already applied in wave 1):

| Artifact | Where it lived | Why it is safe to drop |
|---|---|---|
| `__version__ = "0.1.1"` in code | `gemini_router/__init__.py` | second copy of the pyproject version, no guard existed → now read from package metadata |
| "vX.Y.Z implemented / N tests green" | both READMEs' `## Status` | `--version` and `pytest` already answer both |
| milestone log with per-milestone test counts | `docs/04-plan.md`, `docs/08` `## Follow-ups` | git tags and the issue tracker record this |
| version qualifiers in prose ("since 0.11.0", "(0.8.1)") | `docs/06`, `docs/08`, `docs/09`, `freebuff-wiring`, `runbook` | the behaviour is what the reader needs, not its release |
| pinned version in a copy-paste snippet | `docs/06` `gemini-mcp-gateway==0.8.1` | a stale pin silently installs an old build |
| "Commands (as-built; update me)" | both `AGENTS.md` | the marker is itself an unmaintained promise |

**Keep** — each has tangible benefit beyond the documentation:

| Artifact | Why it stays |
|---|---|
| `pyproject.toml` `version` | the single source of truth; it *is* the artifact |
| `packaging/npm/package.json` `version` | npm requires it, and the shim pins `gemini-mcp-gateway==<version>` |
| the two `release.yml` lockstep guards | protect a real cross-artifact invariant (wheel version == tag, shim pin == published dist), not a prose claim |
| `ledger.SCHEMA_VERSION` | real DB migration marker |
| `MemoryMax=350M`, ports, TTLs, quota limits | operational constants the code reads |

Rule recorded in both `AGENTS.md` files so the next agent does not re-introduce them.
No new CI check was added for this — the point is fewer guarded facts, not more guards.

---

## 1. Priority 1 — Cheaper AI-agent development

### 1.1 Fix the docs that actively mislead · **S** · risk low · **do first**

An agent following today's documentation produces a broken install or a broken client
wiring. Verified discrepancies:

| Where | Says | Reality |
|---|---|---|
| `gemini-router/README.md:41`, `docs/SPEC.md:86` | `uv add "gemini-router @ git+…@v1.0.0"` | remote tags are only `v0.1.0`, `v0.1.1` → **command fails** |
| `gemini-mcp/README.md:80,84,92` | `v0.8.0`, `80 tests` | `pyproject.toml:3` = `0.12.0`; `pytest` = 105 passed |
| `gemini-mcp/README.md` (chain bullet) | `thinking_level` default **high** | `config.py:60` `ThinkingCfg.default = "medium"` |
| `README.md:114,145,146` · `docs/03:186,190,204` · `runbook-workstation.md:55` · `freebuff-wiring.md:14` (7 places) | run `/opt/apps/gemini-mcp/.venv/bin/gemini-mcp` | the unit runs the `uv tool` binary: `setup_server.sh:85,126`, `setup_wizard.py:111` → `~/.local/bin/gemini-mcp` |
| `gemini-mcp/AGENTS.md` | `gemini-mcp serve --http` | `serve` accepts only `--bind` / `--port` (verified via `--help`) |
| `gemini-mcp/AGENTS.md` | `run_remote.sh --setup\|--deploy` | `case` accepts `setup\|deploy` (no dashes) |
| `docs/03-architecture.md:102-117` (called "single canonical definition" by `gemini-router/docs/SPEC.md` §3) | `cooldowns.until_ts TEXT`; `cache.ts TEXT`, different column order; only 2 indexes; status list `ok\|quota\|http4xx\|http5xx\|network`; RPD set `('ok','quota','http5xx')` | `ledger.py:25-44`: `until_ts REAL`, `ts REAL`, `idx_calls_key_model_ts` exists, statuses also `planned\|blocked\|skipped`, RPD set includes `blocked` |

Action: correct all of the above in one `docs:` commit. Delete volatile facts (version
numbers, test counts) rather than updating them — the owner's global AGENTS.md already
mandates exactly this ("omit volatile numeric facts in evergreen docs").

Also serves Priority 4 (P4.1, P4.2).

### 1.2 Collapse the stage-doc set · **M** · risk low

`docs/01..04` are pre-build artifacts and a milestone log ("✅ done (43 tests green)");
`docs/ops/vps-changes-2026-10-09.md`, `…-10-10.md` and `docs/postmortems/p01-…` are
one-off reports. The owner's global convention (`docs/worklog/<date>-<slug>/`, closed →
`archive/`) is not used in either repo — there is no `docs/worklog/` at all.

Action: move superseded artifacts to `docs/worklog/archive/` (move, do not delete);
keep in the default read path only `README.md`, one canonical design doc (merge the
still-true parts of `02-requirements` + `03-architecture`), `06-consumer`, `08-dual-mode`,
`09-ci`, `runbook-workstation`.

Target: ≈ 1 200–1 400 doc lines out of the agent's default path, one source of truth per
topic instead of four partially overlapping ones.

### 1.3 Delete `MemLedger` · **S** · risk low

`src/gemini_mcp/backends.py:25-180` re-implements the whole `gemini_router.Ledger`
contract (155 lines). It is **dead in production**:

- `vulture --min-confidence 60` flags every one of its 9 methods (`plan`, `resolve`,
  `rpd_spent`, `window_usage`, `last_call_ts`, `cooldown`, `set_cooldown`, `cache_get`,
  `cache_put`, `close`) as unused.
- `make_routers` (the only production factory) uses the SQLite `Ledger` for both
  `direct` and `router` modes (`backends.py:212-222`).
- The single remaining call site, `setup_wizard._probe_keys` (`setup_wizard.py:318-331`),
  calls only `Router.availability()`, which never touches the ledger — any object
  satisfies the constructor.

Action: replace with an ~8-line no-op ledger for the probe; drop the MemLedger parity
tests in `tests/test_direct_backend.py` (~60 lines, keep the `resolve_mode`/
`make_routers` tests); update `docs/08-dual-mode.md:21,69`.

Net ≈ **−215 lines** and, more importantly, one fewer copy of quota accounting that can
silently drift from `Ledger`. This is the highest-risk class of duplication in the codebase.

### 1.4 De-duplicate the CLI layer · **M** · risk low-medium

`gemini-mcp/cli.py` `_run_quota` (107-123), `_run_calls` (126-144) and `_doctor_direct`
(147-193) re-implement rendering that already exists in `gemini_router/cli.py`
(`cmd_quota`, `cmd_calls`, `cmd_doctor`) — with different output and ~40 duplicated
doctor lines.

Action: build a `RouterConfig` with the direct-mode `state_path` and delegate; keep only
the direct-mode-specific header line. Check `tests/test_cli_env.py` afterwards.

Net ≈ −90–120 lines, one place to change doctor output.

### 1.5 Dead code and redundant guards · **S** · risk none

| Location | Issue |
|---|---|
| `gemini-mcp/config.py:75` + `cli.py:31` | `backend_resolved` is written, never read (verified: only 2 refs, both writes/decl) |
| `gemini-router/errors.py:31-32` | `"quota exhausted: " + "…".join(…) or "quota exhausted"` — the `or` branch is unreachable; verified `str(e) == "quota exhausted: "` for empty `per_model` |
| `gemini-mcp/pipeline.py:204,209` | `getattr(self.router.cfg, "overflow_tiers", [])` / `getattr(…, "overflow_limits", None)` on a pydantic model that always has both |
| `gemini-router/cli.py:71-75` | `--json` and the default branch of `cmd_models` both emit JSON |
| `gemini-router/events.py:8` vs `docs/SPEC.md` §2 | `STAGES` includes `pacing`, SPEC omits it |
| `gemini-router/tests/test_cli.py:20,28,34,41` | 4 unused `env` fixtures (vulture, 100 % confidence) |

### 1.6 CI/CD · **S** · risk low

Wall time is already 13–20 s. The wins are redundant work and honest signals:

- `gemini-mcp/.github/workflows/ci.yml:2` triggers on every `push`, **tags included** →
  on a release tag, `ci.test` and `release.test` run the identical job. Scope `ci` to
  `branches: [main]` + PRs → −1 job per release.
- `release.yml` `smoke` job: 12 × 30 s PyPI + 16 × 30 s npm = up to 14 min worst case,
  and every retry is a full `uvx --refresh` re-resolve. Poll registry metadata for the
  version instead → ~2 min worst case.
- Optional: split router lint into its own job with `uvx ruff` (no dep install), as
  `gemini-mcp` already does.

**Explicit non-goals:** no test matrix, no coverage gate, no cache layers, no parallel
sharding. The suites finish in 2 s; runner time is dominated by `uv` bootstrap, and the
added machinery would cost more agent context than it saves.

---

## 2. Priority 2 — Hidden bugs and uncovered edge cases

### 2.1 A 401 on one key kills the whole request · **S** · risk low · **highest-value fix**

Reproduced: pool `["bad", "good"]`, first key answers 401 → `RouterError: gemini-3.8-flash:
HTTP 401: {"error":{"message":"API key not valid"}}`, **1** `generateContent` attempt, the
good second key never tried.

Cause: 401 is in neither `RETRYABLE_STATUS` nor `MODEL_SWITCH_STATUS`
(`errors.py:5-6`), so `_try_candidate` falls through to `raise RouterError`
(`routing.py:395`) instead of returning "next candidate".

The product's core premise is a 10-key pool; one revoked or expired key currently takes
down every request. Fix: classify per-key auth failures (401, and 403 carrying an auth
message) as "skip this key, continue", record `http4xx`; raise only when all keys are
exhausted. Add a multi-key 401 test.

### 2.2 `enforce_audit_size` cannot reach its cap · **S** · risk low-medium

Reproduced: 20 000 rows, 2.5 MB file, `max_mb=1.0` → `file 2.5 MB -> 2.5 MB, window
shrunk to 1.0 days, rows left = 0, cap met = False`. SQLite `DELETE` does not shrink the
file, so the loop runs 12 passes, deletes *everything*, and still misses the cap.

Fix: `PRAGMA auto_vacuum=INCREMENTAL` + `incremental_vacuum` (or one `VACUUM` at the end
of `prune`), and cap the loop. Do it in `prune`, never per request. Currently untested.

### 2.3 `/v1/complete` returns wrong status codes and malformed bodies · **S** · risk low

Reproduced against the real ASGI app:

- validation error `raw_document_inlined` → **HTTP 429** with
  `{"error":"{\"error\": \"raw_document_inlined\", …"}` — a JSON *string* nested in
  `error`, not the structured payload.
- body without `prompt` → unhandled `KeyError: 'prompt'` at `server.py:301` → 500 with a
  traceback.

Both break the owner's own rule ("all machine-parsed endpoints must return consistent
JSON errors — not `text/plain`") and tell a consuming agent "rate limited, retry later"
for a request that will never succeed. Fix: validate the JSON body → structured 400;
map `ToolError.error` codes to 400/413/422; keep 429 for quota only; return the payload
object, not a stringified one. Cross-ref P4.4.

### 2.4 `_inline_doc_guard` false positives · **S** · risk low

Reproduced: a legitimate text body that merely *quotes* `%PDF-1.7` mid-document is
rejected with `raw_document_inlined`. The marker scan (`tools.py:126-154`) runs over the
whole field. Real containers always start at byte 0 → scan only the first ~2–4 KB.

### 2.5 Token estimate is ~2× optimistic for non-ASCII · **M** · risk low

`estimate_tokens` = `len(text)//4` (`config.py:97-98`); Cyrillic is ≈ 2 chars/token.
`gemini_generate` never calls `countTokens`, so the TPM pre-flight gate
(`gates_for`, `routing.py:73`) is systematically under-estimated for Russian text →
avoidable 429s. Fix: non-ASCII-aware estimate, or reuse the chars/token ratio measured
by the last real `usageMetadata`.

### 2.6 Ledger concurrency edges · **M** · risk medium

- **RPD race:** the RPD gate excludes `planned` rows (`ledger.py:102-111`) while the
  TPM/RPM windows include them (`WINDOW_STATUSES`). N concurrent processes can all pass
  the RPD check. The MCP pipeline itself is sequential, so the exposure is exactly the
  stated design case: daemon + cron + CLI sharing one `state.db`.
- **Read path takes the write lock:** `cooldown()` issues a `DELETE` + `COMMIT` on every
  read. Measured: one `quota_table()` = **60 `DELETE` + 60 `COMMIT`** (see §3.3). On a
  shared WAL file that is pure contention for a read-only operation.

Fix: make `cooldown()` a plain `SELECT`, move expiry cleanup to `prune`; decide
explicitly whether recent `planned` rows count toward RPD, and pin the decision with a
test. Medium risk because it changes accounting semantics — the existing dual-gate tests
must stay green.

### 2.7 Ingest edges · **S** · risk low

`max_file_mb` has no test; the "one-time ticket" guarantee rests only on a file-exists
check (`ingest.py:68-70`) and is replayable inside the 15-min TTL; `prune()` runs only on
ticket issuance. Add the missing tests and mark the ticket consumed.

### 2.8 Test gaps for exactly the paths above · **S**

No test today for `enforce_audit_size`, `/v1/complete` error mapping, `max_file_mb`, or a
401 inside a multi-key pool (verified by grepping `tests/`). Add 4–6 focused tests
together with the fixes — respx/in-memory only, keeps CI at ~2 s.

---

## 3. Priority 3 — E2E performance and memory

### 3.1 Sync CPU/IO inside `async` handlers stalls the whole daemon · **S** · risk low · **best perf/cost ratio**

`analyze_file_tool` runs `path.read_bytes()`, `sniff_binary` and `extract_text` inline in
an `async def` (`tools.py:255-272`). `extract_text` calls either
`pandoc_to_markdown` → blocking `subprocess.run` with a **60 s** timeout
(`ingest.py:168-170`) or `rtf_to_text` → pure-Python parsing (`ingest.py:190-266`).
`split_text` is likewise sync inside `Pipeline._chunked`.

Measured: `rtf_to_text` on a 3.1 MB RTF = **2.0 s**; linearly ≈ 30 s at the 50 MB
`max_file_mb` cap. For that whole window the single-threaded loop cannot answer
`/healthz` (which push-CD polls), MCP keep-alives, or any other client.

Fix: wrap extraction/decode/chunking in `await asyncio.to_thread(...)`.

### 3.2 Peak memory ≈ 6× body size on a 350 MB-capped unit · **M** · risk medium

Measured on a 9 M-char (18 MB) Cyrillic body:

| step | extra |
|---|---|
| `"…task…" + "\n\n" + body` for `countTokens` (`pipeline.py:91`) | +18 MB full copy |
| `split_text` output (28 chunks, 10 M chars) | +20 MB |
| per-chunk prompts (fresh copy per chunk) | +20 MB total |
| `tracemalloc` peak additional | **56 MB** |

Extrapolated to the 50 MB ingest cap, the peak lands at or above `MemoryMax=350M`
(`setup_server.sh:129`) → the OOM killer takes the daemon on the production external network services host.

Fixes, cheapest first: (a) stop concatenating task + body for `countTokens` — measure the
body alone and add a task estimate, or measure one chunk and extrapolate;
(b) `del` the source body before the chunk loop; (c) lower `max_file_mb` to what the unit
can actually hold (≈ 20 MB) or decode lazily. Medium risk: the budget pre-flight must
stay honest.

### 3.3 Ledger query amplification · **M** · risk medium · do together with 2.6

Measured with 10 keys × 5 chain models, a 2 000-row ledger:

- one `model_budgets()` = **300 SQL statements, 4.1 ms**
- one `quota_table()` = **360 statements** (incl. 60 DELETE + 60 COMMIT)

It runs per tool call *and* per chunk (`routing.py:248` `quota_after`,
`pipeline.py:121` meta, `pipeline.py:202` `_available_rpd`) → ~3 000–6 000 statements per
large job.

Fix: one aggregate `GROUP BY key_id, model` query per table build, plus a short-lived
per-request gate cache. ~10–50× fewer round trips.

Honest caveat: in absolute terms this is 4 ms on a fast machine. The real argument is
(a) WAL write-lock contention across processes on the *shared* ledger and (b) the 1 vCPU
production box — which is why it belongs with the ledger work in 2.6, not before it.

### 3.4 Sequential `listModels` per key on cache miss · **S** · risk low

`availability()` loops keys serially (`routing.py:131-135`). With 10 keys that is 10 round
trips before the first request of each 12 h window. `asyncio.gather` → one round trip of
latency.

**Explicit non-goals:** no streaming-API rewrite, no process pool, no Rust/C extractor,
no cache layer in front of the router. The items above are 10–50× wins; anything further
costs more than it returns at this scale.

---

## 4. Priority 4 — Experience of consuming AI agents

### 4.1 The documented install command does not work · **S**

`git+https://github.com/vernikr/gemini-router.git@v1.0.0` — `v1.0.0` does not exist
(remote tags: `v0.1.0`, `v0.1.1`). Point at the published PyPI dist plus the real tag,
and put the one-line install in both READMEs. Same edit as 1.1.

### 4.2 Make the client wiring copy-pasteable · **M**

The 7 stale `.venv/bin/gemini-mcp` references (table in 1.1) are exactly the string an
agent pastes into `mcp.json` — it produces an entry that looks correct and fails at
spawn, which is the most expensive class of failure to diagnose remotely. Single source
of truth: generate the snippet from `_tool_bin()` / `_client_snippets()`
(`setup_wizard.py:109-150`) and have the docs reference it instead of hand-writing JSON
in four files.

### 4.3 `gemini-mcp smoke` as a CLI subcommand · **M** · risk low-medium

Today `scripts/smoke.sh:7` sets `BIN="$APP/.venv/bin"` and uses it at `:12,16,29,44`
(`$BIN/gemini-mcp`, `$BIN/python`), while the systemd unit runs the `uv tool` binary at
`~/.local/bin/gemini-mcp` (`setup_server.sh:85,126`). **The smoke validates a different
build than the one in production** — it can pass while the deployed release is broken.

`setup_server.sh` also installs the package twice: the source venv (`:72-79`) and the
released wheel (`:87-91`), on a 1 vCPU / 20 GB host whose only consumer of the venv is
`smoke.sh`.

Fix: add `gemini-mcp smoke` (doctor → `countTokens` → one tiny `generate` → quota delta),
so it exercises the shipped binary; reduce `smoke.sh` to a wrapper; drop the source venv
install. Faster provisioning, less disk, and a verification that actually means something.

### 4.4 Errors that name the fix · **M**

Already the project's strongest feature (structured `quota_exhausted` /
`plan_budget_insufficient` / `raw_document_inlined`, each with a `fix` field). Extend the
same shape to the REST surface (2.3) and to `serve`'s missing-token path; make `doctor`
distinguish "no keys" / "no state file" / "`listModels` failed" / "chain not live" in
both message and exit code.

### 4.5 Trim repeated tool-schema prose · **S** · risk low-medium

`analyze_large` / `analyze_file` each carry 15 parameters (14 + `ctx`) with long
docstrings, `gemini_generate` 12; and `INSTRUCTIONS` (**2 559 chars**, `server.py:32-68`)
is sent to every client on connect. The
file-flow recipe is currently stated four times: `INSTRUCTIONS`, `quota_status.file_flow`
(`tools.py:312-316`), `_inline_doc_guard.fix` (`tools.py:145-153`) and README. Keep the
payload-borne hints (they were added after real incidents — postmortem p01) and cut the
prose duplication only.

---

## 5. Sequencing

| Wave | Tasks | Est. | Status |
|---|---|---|---|
| **1** | 1.1, 1.5, 2.1, 2.3, 2.4, 3.1, 4.1 + §0.1 | 0.5–1 day | **SHIPPED** — see §6 |
| **2** — structural cleanup, docs, CI | 1.2, 1.3, 1.4, 1.6, 2.2, 2.7, 2.8, 4.2 | 1–1.5 days | pending owner answer on archiving `docs/01..04` |
| **3** — semantics-touching, needs the new tests green first | 2.5, 2.6, 3.2, 3.3, 3.4, 4.3, 4.4, 4.5 | 2–3 days | pending owner answer on the 50 MB ingest cap |

### Verification per wave

```bash
# gemini-router
uv sync --extra dev && uv run ruff check src tests && uv run pytest -q
# gemini-mcp — exactly what a consumer installs (public registries, no private git)
uv venv .venv-ci && UV_NO_SOURCES=1 uv pip install --python .venv-ci/bin/python -e ".[dev]"
.venv-ci/bin/ruff check . && .venv-ci/bin/pytest -q
# after wave 1/2: bash scripts/run_remote.sh smoke   (spends 1 RPD; must use the uv-tool binary)
```

Baseline to beat: 45 passed / 1.95 s and 105 passed / 2.01 s. Every wave must add tests
for the bug it fixes; no wave may add a test that needs the network.

### Deliberately out of scope

Multi-provider abstraction (groq/openrouter) — `SPEC.md` §6 already defers it and nothing
requires it. Further repo splitting. More config surface. Any new framework dependency
(`AGENTS.md` pins router to `httpx`/`pydantic`/`rich`/stdlib). Coverage gates and CI
matrices. These would add agent context cost without a matching return.

---

## 6. Wave 1 — shipped 2026-10-10

`gemini-router` 0.1.1 → **0.1.2** · `gemini-mcp` 0.12.0 → **0.12.1** (+ npm shim 0.12.1)

| Fix | Before (measured) | After (measured) |
|---|---|---|
| 2.1 refused key in a pool | 1 `generateContent` attempt, `RouterError` raised, remaining keys never tried | 2 attempts, result returned from the healthy key; an all-dead pool raises "every configured API key was rejected … rotate …" |
| 2.3 REST error honesty | `raw_document_inlined` → **429** with a stringified payload; missing `prompt` → `KeyError` → 500 traceback | 400 with the payload object; `missing_prompt` 400; non-JSON body 400; quota stays 429 |
| 2.4 doc-guard false positive | a text quoting `%PDF-1.7` mid-document was **rejected** | accepted; a leading signature is still rejected |
| 3.1 daemon freeze during ingest | event loop stalled **2.05 s** for a 2 s extraction | **0.10 s** stall for the same work |
| 1.5 `QuotaExhaustedError` message | `"quota exhausted: "` (dangling separator) | `"quota exhausted"` |
| 4.1 install | `git+…@v1.0.0` — tag does not exist | `uv add gemini-router`; consumer install verified from PyPI |
| §0.1 counters | 7 stale `.venv` binary paths, `==0.8.1` pin, `thinking default high` vs `medium`, version/test-count blocks in both READMEs, `serve --http` and `run_remote.sh --setup` (neither exists) | removed; `docs/03` §3 DDL verified table-by-table against `ledger.py` (4/4 match, 3/3 indexes, RPD set matches) |

New tests: 401 fall-through, all-keys-refused message, error-message regression, REST
status mapping (4 cases), head-only guard. `gemini-router` 45 → **48 passed / 1.85 s**;
`gemini-mcp` 105 → **107 passed / 1.78 s**. Ruff clean in both.

Full CI/CD, both repos:

- `gemini-router`: `ci` green on `main` and `v0.1.2`; 0.1.2 published to PyPI; a
  `release.yml` was added (test → PyPI via OIDC + the same lockstep guard) — it needs the
  one-time trusted-publisher setup from `gemini-mcp/docs/09-ci.md` §1 before it can
  publish on its own.
- `gemini-mcp`: `ci` green on `main`; tag `v0.12.1` → **all 5 release jobs green**
  (test 15 s, pypi 39 s, npm 9 s, deploy 46 s, smoke 2 m 44 s).
  Deploy log: `previous=0.12.0` → `OK: healthy, on 0.12.1` (two registry-lag retries,
  no rollback).
- Consumer check from public registries: `gemini-mcp-gateway==0.12.1` resolves
  `gemini-router 0.1.2` on its own, `--version` reports 0.12.1, `doctor --offline` runs.

---

## 7 · Wave 2 — shipped 2026-10-10

Scope: 1.2 (archive stage docs), 1.3 (delete `MemLedger`), 1.6 (instruction
deduplication), 2.2 (audit size cap), 2.7 (smoke binary), 2.8 (upload cap), 4.2
(client-JSON deduplication), plus 1.4 scoped down (see below).

### Corrections to this plan, found while implementing

Two entries described code that does not exist in that shape. Recorded so the next
reader does not trust them:

- **2.2 was wrong about the function.** The plan placed `enforce_audit_size` in
  `ledger_tools.py` with a `window` parameter, a 16-pass loop and a `cap_met` dict
  result. The real function was in `logging_setup.py:128`, took `(ledger, max_mb)`,
  looped `3650 → 1` days and returned a `float`. The *conclusion* held — the cap was
  unreachable and untested — but everything about its shape was invented.
- **The planned fix would not have worked.** `Ledger.vacuum()` as a bare
  `VACUUM` statement reclaims nothing under WAL: the result sits in the write-ahead
  log until a checkpoint. Measured on a 536 576-byte ledger emptied by `prune`:
  `VACUUM` alone → 536 576 bytes (unchanged); `VACUUM` + `wal_checkpoint(TRUNCATE)`
  → 45 056 bytes, `freelist_count` 0. The shipped `Ledger.vacuum()` does both.

### What shipped

| Task | Before | After |
|---|---|---|
| 2.2 audit size cap | `enforce_audit_size` halved the retention window 3650 → 1 days and returned with the file **still over the cap**; DELETE never shrinks SQLite, so it could never have met it. Untested. | prune → `Ledger.vacuum()` → measure, stepping 30/7/1 days and reporting `{cap_met, keep_days_used, size_mb}` honestly. Covered by a test that fills a real ledger. |
| 1.3 `MemLedger` | 155 LOC duplicate of the SQLite ledger + 2 parity tests re-proving rules already covered by `gemini-router/tests/test_ledger.py` (8 tests) | deleted. `state_path: ":memory:"` gives the real `Ledger` a zero-disk database — verified: accepts writes, `stats()['calls'] == 1`, creates no files. `backends.py` 223 → 64 LOC. |
| 2.8 upload cap | `/v1/upload` called `await request.body()` **before** any size check — one oversized upload could consume the whole `MemoryMax=350M` budget before being refused. The ticket is documented as "bound to declared size" but nothing ever compared the body to it. | `Content-Length` and the ticket's declared size are both checked before buffering; a body whose length differs from the declared size is rejected as `size_mismatch`. Measured: a 200 MB declared upload is refused in **4.7 ms**, RSS peak +0.3 MB, nothing written. |
| 2.7 smoke binary | `smoke.sh` ran `$APP/.venv/bin/gemini-mcp` while systemd runs `~/.local/bin/gemini-mcp` — the smoke test verified a binary that is not the one serving traffic | resolves the deployed binary from the passwd entry (so `sudo`'s `HOME` handling cannot mislead it) and uses that tool's own interpreter for the inline steps |
| 1.6 instruction duplication | the file-ingest recipe was written out four times (server instructions, tool docstring, tool result, upload response) and `analyze_large` restated the pipeline the server instructions already carry | one canonical copy in `INSTRUCTIONS` plus the two places that carry actionable payloads (the signed URL, the `next` hint). Tool schema **5 718 → 5 214 chars (−8.8 %)**; per-argument docs kept, since they are read at the point of use. |
| 4.2 client JSON | the SSH-stdio block appeared 4× and the HTTP block 3× across README, `docs/03`, the runbook and `freebuff-wiring.md`; the setup wizard already writes these files itself | `freebuff-wiring.md` is canonical (it is the doc dedicated to fleet wiring — a better home than `docs/06`, which covers the direct/npx path); `docs/03` and the runbook now point at it. README keeps one copy as the front door. |
| 1.2 stage docs | `docs/` mixed live contract with history: `01-unpacking-reframing`, `04-plan`, `05-packaging`, `ops/`, `postmortems/` — ~578 lines nothing depended on | moved to `docs/worklog/archive/` per the owner's convention; files not renumbered. **`docs/02-requirements.md` stays** — 25 live `FR-x` references in `src/` and `tests/` make it the traceability dictionary, not history. Six inbound links repaired (README stage table, two `AGENTS.md` lines, `docs/02`, `docs/06`, `docs/09`, plus `gemini-router`'s `AGENTS.md` and `SPEC.md`); post-move grep finds no dangling reference. |

### 1.4 scoped down — and why

The plan called for dropping `$APP/.venv` from `setup_server.sh` as a duplicate
install. After 2.7 landed, nothing consumes that venv: systemd runs
`~/.local/bin/gemini-mcp` and smoke.sh now does too.

It is still load-bearing for one case. `uv tool install gemini-mcp-gateway==<ver>`
resolves `gemini-router` from PyPI, so while a router version is unpublished the tool
path cannot work — and the venv path installs the router from the local checkout.
Router releases are manual for now, which makes that window common rather than rare.
Removing it would trade ~20 s of deploy time for a setup script that fails whenever a
router is ahead of PyPI, and the VPS path cannot be rehearsed from here.

Kept, with the comment rewritten to say what it is actually for so the next reader does
not delete it as dead weight.

### Verification

`gemini-router`: **49 passed / 1.89 s**, ruff clean (was 48). New test
`test_vacuum_reclaims_file_space` asserts the file size is unchanged after `prune` and
shrinks by >4× after `vacuum()` — the assertion that would have caught the WAL detail.

`gemini-mcp`: **107 passed / 1.97 s**, ruff clean. Two MemLedger parity tests removed
(coverage retained in the router's own suite); three added — audit cap, upload cap
(four cases), and the direct-mode E2E tests now run on the real SQLite `Ledger` instead
of the duplicate, which is a stronger test than the one they replaced.

`gemini-mcp` now requires `gemini-router>=0.1.3`: `enforce_audit_size` calls
`Ledger.vacuum()`, and the previous floor (`>=0.1.1`) would have let a consumer resolve
a router without it and hit `AttributeError` at prune time.

### Full CI/CD

`gemini-router` **0.1.3** — `ci` green on `main` and `v0.1.3`. The `release`
workflow's `test` job is green but its `pypi` job fails with
`invalid-publisher: valid token, but no corresponding publisher`, i.e. the one-time
PyPI trusted-publisher setup that is still outstanding; 0.1.3 was published manually
instead, as agreed.

`gemini-mcp` **0.13.0** — `ci` green on `main`; tag `v0.13.0` → all five release jobs
green: test 13 s, pypi 23 s, npm 8 s, deploy 49 s, smoke 72 s.

Deploy: `previous=0.12.1` → two registry-lag retries → `OK: healthy, on 0.13.0`.
Smoke: both the PyPI and the npm probe reported `gemini-mcp 0.13.0` after one retry
each. Registry lag on the first attempt is now consistent across every release and is
handled by the existing retries, not a defect.

Registries: PyPI `gemini-mcp-gateway 0.13.0`, `gemini-router 0.1.3`,
npm `@vernikr/gemini-mcp 0.13.0`. The published wheel declares
`gemini-router<0.2,>=0.1.3`.

Clean consumer install from public registries: `gemini-mcp-gateway==0.13.0` resolves
`gemini-router 0.1.3` on its own, `Ledger.vacuum` is present, `--version` reports
0.13.0, `doctor --offline` runs clean.

---

## 8 · Measured: what the `max_file_mb` cap actually costs (input for wave 3)

The plan carried an extrapolated claim that a 50 MB ingest peaks at or above
`MemoryMax`. Measured instead, on the real `analyze_large` pipeline with respx-mocked
HTTP (no quota spent), sizing each body by its **UTF-8 byte length** — which is what
`max_file_mb` measures. An earlier run of this probe sized bodies in characters and
therefore understated Cyrillic files by roughly 2×; the numbers below are the
byte-accurate ones.

Idle process: **74.7 MB**. VPS unit: `MemoryMax=350M` (`setup_server.sh:132`), and
`owner deployment notes` fixes background services at 250–350 MB so the OOM killer never
threatens the production external network services — raising the ceiling is not an option.

| Uploaded file (Cyrillic text) | Characters | Chunks | RSS peak | vs 350 MB |
|---|---|---|---|---|
| 10 MB | 5 592 384 | 9 | **130.5 MB** | 37 % |
| 20 MB | 11 184 768 | 18 | **223.0 MB** | 64 % |
| 30 MB | 16 777 200 | 26 | **333.9 MB** | **95 %** |
| 40 MB | 22 369 584 | 35 | **472.5 MB** | **135 % — killed** |

Growth is close to linear at ≈ **11.4 MB of RSS per 1 MB of file**, so the 350 MB
ceiling is crossed at roughly **31 MB**. Concurrency does not double the peak inside
one process — two simultaneous 35 MB jobs peaked at 243.1 MB — because freed chunk
buffers are reused; a single large job is the binding case, not two medium ones.

Two consequences:

1. **The configured `max_file_mb: 50` is unreachable.** The service is OOM-killed near
   31 MB, well before the cap it advertises. The failure mode is the worst available
   one: the whole unit dies and every in-flight job with it, rather than one request
   being refused.
2. **The driver is characters, not bytes.** Cyrillic packs ~2 bytes per character, so
   an English or ASCII document reaches the same character count — and therefore the
   same memory — at about half the file size. Any cap chosen in megabytes is really a
   cap on extracted characters, and is twice as tight for Latin text.

A cap of **20 MB** leaves 127 MB of headroom (64 % of budget) and is honest for both
scripts. The root cause is wave 3 work: the pipeline holds several full copies of the
body at once (`task + body` for `count_tokens`, per-chunk prompt strings, the
consolidation input), and removing those copies is what would make a larger cap
genuinely reachable rather than merely advertised.

---

## 9 · Ingest cap set to a reachable value (shipped 2026-10-10)

Owner's decision after §8: default `max_file_mb` 50 → **20**, plus an escape hatch
that is opt-in at both ends.

- `ingest.max_file_mb: 20` — 223 MB peak against a 350 MB ceiling, 127 MB of
  headroom, honest for Cyrillic and for Latin text alike.
- `ingest.allow_oversize: false` — the owner's switch. While it is off the cap is
  absolute: `override_size_limit` is refused with `oversize_not_permitted` (HTTP 403),
  so no caller can talk its way past it.
- `file_upload_ticket(…, override_size_limit=true)` — works only when the owner has
  opted in. Two keys have to agree before the cap can be exceeded, which is the
  point: an agent cannot authorise itself.
- `file_too_large` now carries a `fix` field naming the three alternatives instead of
  just the numbers, and the MCP instructions carry a SIZE DISCIPLINE block telling an
  agent to (1) re-save as plain text or Markdown — RTF/DOCX markup often outweighs the
  words, and archives are not accepted; (2) split across separate uploads and
  `analyze_file` calls and combine the answers, reporting the separate charges; (3)
  only then ask the user for explicit permission, stating plainly that the service may
  be killed mid-job and take other work with it. Instructions grew 2 559 → 3 722
  chars; this is the one canonical copy, and `quota_status.file_flow` was deliberately
  left alone rather than duplicating it.

`INSTRUCTIONS` is the only place the guidance lives; `docs/03` records why the cap is
a memory guard rather than a provider limit, and `docs/06` records the measured
11 MB-per-MB cost so the next reader can re-derive the number instead of trusting it.

### Provider limits, checked rather than guessed

The 20 MB vs 100 MB disagreement in secondary sources is a conflation of three
different numbers. Per `ai.google.dev/gemini-api/docs/files`: inline request payloads
(including the prompt) are capped at **100 MB**, or **50 MB for PDFs**; the **Files
API** stages up to **2 GB per file** and **20 GB per project** for **48 hours**. The
20 MB figure is the *former* inline limit, and separately a client-side cap in
`gemini-cli`'s own `read_file` tool — neither is a current API limit.

None of them bind here. Grepping both repos for `inline_data`, `fileData`, `file_uri`,
`base64` and the Files upload endpoint returns nothing: this gateway never sends file
bytes to the provider. `analyze_file` extracts text on the server and feeds the
chunking pipeline, so the only limit that matters is local RAM — which is exactly what
§8 measured. Adopting the Files API would be a way to send *binary* payloads (PDF,
images, audio) to the model directly; it would not raise the ceiling on text analysis,
because text analysis is not payload-bound.

### The VPS still needs one command

The new default is in the package, but the VPS does not read the package for this: it
reads `/opt/apps/gemini-mcp/config.yaml`, which push-CD never touches (the CD path
reinstalls the tool binary only). That file still carries `max_file_mb: 50`, so the
production service is still running the unreachable cap until the config is synced:

```bash
bash scripts/run_remote.sh setup     # rsyncs config.yaml + re-provisions, idempotent
```

The sandbox has no SSH key for the VPS (`docs/blockers/b05`), so this one cannot be
done from here.

---

## 10 · Wave 3 — implemented and verified, release blocked 2026-10-10

Everything below is committed locally and green (router 50 tests, gateway 110 tests,
ruff clean in both), but **not pushed and not published**: the GitHub PAT in the
owner's AGENTS.md started returning HTTP 401 part-way through this wave, after it had
already served the 0.14.0 release. See `docs/blockers/b06-github-pat-revoked.md`.
Versions are therefore still 0.14.0 / 0.1.3 on disk; the bump, the lock update, the
tags and the CD run are the only steps left, and they need a working token.

Scope: the SQLite statement count on the budget path, and the memory the pipeline
holds while chunking (the binding constraint on `max_file_mb`, per §8).

### Budget queries: 360 → 3

`Ledger.cooldown()` ran `DELETE FROM cooldowns WHERE until_ts<=?` inside a
transaction on **every read**. It is called once per key×model pair on every budget
check, so a sixty-pair table issued 60 BEGIN + 60 DELETE + 60 COMMIT to garbage-collect
a table that `prune()` already cleans. Made it a pure read (`until_ts>?`); the
arithmetic is unchanged and the 49 existing tests pass untouched.

Then `quota_table()` and `model_budgets()` were rebuilt on a new
`Ledger.gate_snapshot()`, which reads every pair's three gate inputs in three grouped
queries instead of three queries per pair. The gate arithmetic was extracted into
`gates_from()` and is shared by both paths, so there is still exactly one copy of it.

| | before | after |
|---|---|---|
| `quota_table()` (60 rows) | 360 stmts / 3.7 ms | **3 stmts / 1.0 ms** |
| `model_budgets()` (5 models) | 300 stmts / 2.4 ms | **3 stmts / 0.4 ms** |
| whole 134-chunk job | ~26 000 stmts | **9 943 stmts** (8 137 SELECT) |

The two paths run different SQL, so they can drift; a new test asserts the snapshot
returns exactly what the per-pair accessors return, over 300 scattered rows mixing
ok/error/quota statuses, `count` rows, and live plus expired cooldowns.

The remaining SELECTs are the per-attempt routing decisions, which have to be fresh.
Batching those would trade correctness for speed and was left alone.

### Pipeline memory: 4× the document → about 1.7×

`tracemalloc` put the pipeline's transient allocations at **4× the body size**. The
dominant item was `chunker.segment()` returning a list: for a 25.7 MB document it
retained **44.1 MB across 300 000 segment objects** — the per-object overhead exceeds
the text. `split_text` then held that list and the finished chunks at the same time.

`segment()` is now a generator and `split_text` keeps only the chunk under
construction plus the overlap tail. Isolated: RSS above the body after `split_text`
went **+52.8 MB → +5.0 MB**.

Two smaller copies were removed on the way: `count_tokens(p.task + "\n\n" + body)`
built a second full copy of the document to measure it — and the request itself sends
the task as `system`, not glued to the prompt, so measuring the body alone is both
cheaper and closer to what is actually billed. `PARA_RE`/`SENT_RE` switched from
`findall` to `finditer` for the same reason.

Honest note: those two did **not** move the peak. Measured, the peak was unchanged to
within noise, because the copies were freed before the peak moment during chunking.
They cut allocation churn and one inaccuracy; the generator is what moved the peak.

### End-to-end peak, each size in its own process

| Uploaded file | wave 2 | wave 3 | change |
|---|---|---|---|
| 10 MB | 130.5 MB | **127.5 MB** | −3.0 |
| 20 MB | 223.0 MB | **171.4 MB** | −51.6 (−23 %) |
| 30 MB | 333.9 MB | **214.4 MB** | −119.5 (−36 %) |
| 40 MB | 472.5 MB | **257.5 MB** | −215.0 (−46 %) |

Slope fell from ≈9.9 MB of RSS per uploaded MB to ≈4.6, so the 350 MB ceiling moves
from ≈28 MB to ≈**60 MB**. A 40 MB file now fits with 92 MB to spare; before it was
killed outright.

The 20 MB default from §9 stays — it is the owner's decision, and it is now very
conservative: 171 MB peak against a 350 MB ceiling, 178 MB of headroom. There is room
to raise it later without touching the code.

---

## 11 · After the refactor — two items the owner asked to keep

Not part of this refactor. Recorded here so they are not lost when the plan is
archived; both are product work, not cleanup.

### A · Files API support for binary files

Today the gateway never sends file bytes to the model: `analyze_file` extracts text
server-side (pandoc for DOCX/ODT/EPUB/HTML, native for RTF) and feeds the text
pipeline. Anything that is not text fails fast with `binary_file_needs_conversion`.
That is the right call for documents and it is why no provider payload limit binds us
— but it means PDFs, images and audio cannot be analysed at all.

The Files API is how they would be: upload the bytes, reference the returned URI in the
request. Limits, from the provider's own documentation: 2 GB per file, 20 GB per
project, 48-hour retention, and the file cannot be downloaded back — so it is staging,
not storage. Inline payloads are capped separately (100 MB, 50 MB for PDF), which is
where the widely-quoted "20 MB" comes from: that was the old inline figure.

Decisions this needs before code: what to do about the 48-hour expiry when a job is
retried later; whether uploads count against the ledger (they are not generateContent
calls, so they spend no RPD but do cost quota elsewhere); and whether binary analysis
shares the memory envelope measured in §10 or needs its own, since the model does the
heavy lifting instead of our chunker.

### B · Using the router from more places

The owner wants the same Gemini access — keys, quotas, ledger — reachable from a
macOS desktop app, from utilities on the VPS itself (including over SSH), and from
wherever else comes up. The question is whether that is router work, a new bridge
project, or both.

What is already true, and matters for the answer:

- `gemini-router` is a **library**, deliberately dependency-light (`httpx`, `pydantic`,
  `rich`, stdlib) and MCP-agnostic. It already has three consumers: `gemini-mcp`,
  `external-consumer` (as a sync facade), and outsiders. Nothing about it assumes a server.
- Quota truth already lives in a **shared SQLite ledger**, WAL mode with
  `busy_timeout`, and `test_cross_instance_sharing` covers two processes on one file.
  On the VPS that file is `/opt/apps/shared/gemini-router/state.db` and every gateway
  process already shares it.
- `gemini-mcp` is an **MCP server with an HTTP mode** (`serve`, `127.0.0.1:8790`,
  bearer-guarded) and an SSH-stdio mode. A process that speaks neither MCP nor that
  HTTP surface cannot use it today.

So the shape of the answer:

1. **A VPS-internal utility needs nothing new.** It imports `gemini-router`, points
   `state_path` at the shared ledger, and gets correct accounting for free. The only
   work is packaging/ergonomics — a small CLI or a documented import recipe. This is
   router work, and cheap.
2. **A macOS desktop app is the real question.** It could embed the library and talk
   to the shared ledger — but the ledger is on the VPS, and putting a SQLite file on a
   network mount is the wrong way to share a WAL database. The honest options are
   either the existing HTTP surface (already built, already bearer-guarded, needs the
   SSH tunnel the docs already describe) or a new thin service exposing the router over
   plain HTTP/gRPC for non-MCP clients.
3. **A separate bridge project is probably not needed yet.** `gemini-mcp`'s HTTP mode
   already is that bridge; what it lacks is a non-MCP-friendly surface — a plain
   `POST /v1/complete` exists, so a desktop app can use it today. Build the second
   service only when a consumer appears that the existing surface genuinely cannot
   serve, or the two start wanting different lifecycles.

Recommended approach: treat `gemini-router` as the single place quota logic lives and
refuse to let it grow transport concerns; add transports as thin adapters. Concretely,
first make the library pleasant to embed (a documented sync facade and a stable
"point at the shared ledger" recipe — `external-consumer` is the proof it works), then point
the desktop app at the existing HTTP endpoint, and only then consider a dedicated
bridge if a concrete consumer demands one.

### Wave 3 released

The GitHub PAT from §10's blocker was replaced by the owner; the old one had been found
and revoked by GitHub's secret scanning, which is why it died mid-session rather than
expiring.

- `gemini-router` **0.1.4** — `main` + `v0.1.4` pushed, `ci` green on both. **Not on
  PyPI**: the PyPI token in the same exposed file was revoked too (403 on upload), and
  the router has no trusted publisher configured. The tag push itself failed once with
  GitHub's `fatal error in commit_refs` and succeeded on the immediate retry.
- `gemini-mcp` **0.14.1** — all five release jobs green (test 13 s, pypi 32 s, npm 8 s,
  deploy 47 s, smoke 228 s); deploy log `OK: healthy, on 0.14.1`. Both PyPI and npm
  report 0.14.1. It stays on `gemini-router>=0.1.3` because 0.1.4 is not published —
  the two waves are independent, the chunker work needs nothing from the router.

Verified live on the VPS over SSH: unit `active`, `healthz` reports 0.14.1, the
effective `max_file_mb` is **20**, `allow_oversize` is gone from the model, and
`MemoryMax=367001600` (350 MiB). The stale `max_file_mb: 50` in the server's own
`config.yaml` was corrected in place, with a backup in `/root/bak/` — push-CD replaces
the package but never touches that file, so a default change does not reach a deployed
server on its own. Worth remembering: it is the one part of the config that rsync owns.

The router reaching PyPI needs either a fresh token or the one-time trusted-publisher
setup that has been deferred; the second removes the dependency on a token entirely,
which this incident is a good argument for.
