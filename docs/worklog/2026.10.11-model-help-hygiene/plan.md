# MCP tool-help chain hygiene · 2026-10-11

The registered `analyze_large` tool description still enumerated deprecated 3.5/3.7
aliases as if they were real availability-order candidates. The code had removed
these aliases; the host agent's own read path had not. The description now states
the routing policy without copying volatile model ids and points to `quota_status`
for live model names/budgets. A registered-tool schema test rejects the stale
string. This is only an agent-facing hint fix, with no routing changes.

The router's separate README/SPEC chain description is corrected in that project's
`docs/worklog/2026.10.11-model-doc-hygiene/`. Do not duplicate its prose here.

Gateway patch 0.24.2: full tests, lint and secrets guard before tag. Publication
and production version verification recorded after the release.
