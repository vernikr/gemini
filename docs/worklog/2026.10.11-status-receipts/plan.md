# Status receipts · Wave 5 · 2026-10-11

Owner accepted the recommendation after Wave 4. No new feature surface: harden the
human-facing warnings and polling status, then verify the MCP wire. Full Freebuff
Desktop/host-agent chat rendering requires the external UI; see
`docs/blockers/b03-freebuff-progress-support-unverified.md`.

## Changes

- `rough_edges`: one continuous router wait (same key/model/gate) = ONE episode,
  regardless of the 2-second countdown ticks; dispatch or a new gate starts a new
  episode. Retries/switches still count separately. The detailed trace is untouched.
  Receipt says these are not failed requests. A production Wave-4 job's existing
  trace had 84 wait ticks + 3 switches (`rough_edges=87` old); regrouping yields
  3 distinct waits + 3 switches = 6 episodes.
- Percent uses a monotone high-water mark. `analyze_files` maps each nested text
  pipeline and split-PDF pipeline to a bounded parent span; only the final `done`
  reaches 100. A child `done` exposes phase `file_complete` rather than `finishing`.
- ⛔️ cues cover registered compute tools (inline and async), direct upload-ticket
  validation and REST request errors. MCP report lists all distinct models and tags
  each unrequested Lite/Gemma with ⚠️ (the Wave-4 contract), now verified through
  `build_server().call_tool` with a multi-model fake; output serialization retains
  both model names and warning signs.

## Verification boundary

Automated MCP registration/serialization and REST tests, full battery, PyPI/npm/deploy
and production raw MCP calls are in scope. Freebuff Desktop's final *agent prose* is
not observable from this workspace: no installed/running host agent here. Keep the
external UI check in b03 open; do not equate MCP's tool payload with proof that an
uncontrolled host agent will obey it.

## Published and checked

Gateway v0.24.1: 204 tests + ruff + secret guard green; test, PyPI, npm, deploy and
smoke all green; production daemon 0.24.1 active. Real MCP wire: an invalid upload
ticket returned `⛔️` in its tool error. A two-text-file `analyze_files` job polled
for 133s; poll pct never moved backwards or reached 100 while running. Its three
`done` stages were 20% (file 1), 25% (file 2) and 100% (whole job); the final result
listed all distinct models and answered both files. The long run had waits and
switches, reported as distinct episodes, not failed requests. Freebuff's *chat
wording* is still unobserved (b03); no claim of host-agent confirmation.

