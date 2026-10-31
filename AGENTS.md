# AGENTS.md — gemini

Follow the owner's global AGENTS.md (English in files, Russian in chat, PM-style reports,
Conventional Commits, SemVer, push after every successful step, docs updated with
functional changes, and risks documented alongside affected features). Repo-specific rules:

## Context
- The contract is `docs/02-requirements.md` (FR-x) + `docs/03-architecture.md`; do not
  contradict them silently — update them. Worklogs are implementation notes, not contract.
- Router library = separate repo `gemini-router`; quota/ledger logic lives THERE,
  pipeline/MCP/progress/logging lives HERE.
- Never bake a private server hostname/IP, user home path, or infrastructure identifier
  into source, tests, docs, or examples. Do not modify unrelated networking, firewall,
  or host services without explicit owner authorization. Keep server services within
  their configured memory budget; install Python tooling with `uv`, never global pip.

## Commands
- `uv sync` · `uv run pytest` · `uv run ruff check src tests`
- `uv run gemini stdio` / `serve [--bind --port]` / `setup` / `uninstall` /
  `quota` / `calls` / `logs [-f]` / `prune` / `doctor`
- Deploy from a workstation with `bash scripts/run_remote.sh setup|deploy|smoke|status|logs`;
  the server address must be supplied explicitly as `VPS_HOST`.

## Secrets
- Never commit `.env`, keys, tokens, real request bodies, private keys or user credentials.
  `key_id = sha256[:8]` only.
- Runtime credentials belong in a private environment file with mode `0600`; data/ledger
  paths are deployment configuration, not source constants.

## Testing
- No network in CI: respx mocks reproduce the provider's 429/5xx payload shapes.
  Real API only via an explicitly approved smoke test (budget: ≤1 RPD per run).
- Every FR-x from `docs/02` maps to a test; keep the mapping in test docstrings.

## Docs
- Worklog lives in the repo that owns the change (placement rule mirrored in
  gemini-router): a cross-repo feature splits into a capability plan there and a
  product plan here, each stating its half once.
- No counters in evergreen docs: no version numbers, dates, test counts or "N green"
  claims. They go stale silently and each one is a fact somebody must guard. Keep only
  what a reader cannot get from `pyproject.toml`, `--version` or `pytest`.
- The two release-workflow lockstep guards (pyproject version == tag, npm version == tag)
  stay: they protect a real cross-artifact invariant, not a documentation claim.

- For native GitHub sub-issues, ensure `.github/workflows/issue-parent-sync.yml` exists with `issues: [opened, edited]`, `permissions: issues: write`, and a job calling `vernikr/mother/.github/workflows/issue-parent-sync.yml@v1` (create the caller workflow if missing); then put `<!-- issue-parent: #N -->` on its own line in the child Issue body, or use `<!-- issue-parent: none -->` to detach.
