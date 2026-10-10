# AGENTS.md — gemini-mcp

Follow the owner's global AGENTS.md (English in files, Russian in chat, PM-style reports,
Conventional Commits, SemVer, push after every successful step, docs updated with
functional changes, blockers in `docs/blockers/`). Repo-specific rules:

## Context
- The contract is `docs/02-requirements.md` (FR-x) + `docs/03-architecture.md`; do not
  contradict them silently — update them. Superseded stage docs live in
  `docs/worklog/archive/` and are history, not contract.
- Router library = separate repo `gemini-router` (pinned by git tag in pyproject);
  quota/ledger logic lives THERE, pipeline/MCP/progress/logging lives HERE.
- Target host: VPS YOUR_VPS_HOST (Ubuntu 24.04, 1 vCPU, 1 GB RAM, production external network services).
  HARD RULES (from owner deployment notes): never touch ports 443/20724, `unrelated-service`,
  `/usr/local/external-service`, `/etc/sysctl.d/99-external network services-bbr.conf`, `system resolver configuration`; never block
  22/443/20724 in ufw; units get `MemoryMax=350M`; install via `uv` `--no-cache` into
  `/opt/apps/gemini-mcp/.venv` (PEP 668 — no global pip).

## Commands
- `uv sync` · `uv run pytest` · `uv run ruff check src tests`
- `uv run gemini-mcp stdio` / `serve [--bind --port]` / `setup` / `uninstall` /
  `quota` / `calls` / `logs [-f]` / `prune` / `doctor`
- Deploy (user's workstation): `bash scripts/run_remote.sh setup|deploy|smoke|status|logs`
- On the VPS the daemon runs the `uv tool` binary `~/.local/bin/gemini-mcp`, NOT a
  project `.venv` — docs and client snippets must use that path.

## Secrets
- Never commit `.env`, keys, tokens, real request bodies. `key_id = sha256[:8]` only.
- Keys on server: `/opt/apps/gemini-mcp/.env` (chmod 600), pool imported from
  `/opt/apps/external-consumer/.env`. Shared state: `/opt/apps/shared/gemini-router/state.db`.

## Testing
- No network in CI: respx mocks reproduce the provider's 429/5xx payload shapes.
  Real API only via
  `scripts/smoke.sh` on the server (budget: ≤2 RPD per run).
- Every FR-x from `docs/02` maps to a test; keep the mapping in test docstrings.

## Docs
- No counters in evergreen docs: no version numbers, dates, test counts or "N green"
  claims. They go stale silently and each one is a fact somebody must guard. Keep only
  what a reader cannot get from `pyproject.toml`, `--version` or `pytest`.
- The two release-workflow lockstep guards (pyproject version == tag, npm version == tag)
  stay: they protect a real cross-artifact invariant, not a documentation claim.
