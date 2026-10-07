# gemini-mcp

MCP gateway for **massive-payload Gemini analysis on the AI Studio free tier**.
Runs on a small VPS; a local Freebuff Desktop agent sends huge bodies to it and gets back
a compact consolidated result — the agent's own context stays clean.

- Cascading Flash chain `3.8 → 3.7 → 3.6 → 3.5 → 3` via
  [gemini-router](https://github.com/vernikr/gemini-router) (per key×model RPD/RPM/TPM
  ledger shared with `external-consumer`).
- Over-limit bodies: split into overlapping chunks, each marked `[CHUNK N/M]`, analyzed
  separately, then consolidated (recursively if needed).
- `thinking_level` default **high**, per-request override; opt-in overflow
  (`flash-lite → gemma`) when the daily quota is gone; hard stop otherwise; per-request
  wait policy for minute-limit resets (default: wait, countdown reported in progress).
- Deep progress to the caller (MCP progress + logging notifications + embedded trace),
  rotating disk logs, `gemini-mcp quota|logs|calls|doctor` in the terminal.
- Transports: SSH-stdio (primary) and streamable HTTP daemon (`127.0.0.1`, bearer).

## Status

**Stages 1–4 done; M0–M3 shipped → v0.2.0.** Router core lives in `gemini-router`
(v0.1.0, pinned git dep). This repo: MCP server (stdio + HTTP daemon), 4 tools,
chunking pipeline with recursive consolidation, 3-level progress funnel, rotating
logs + JSONL audit, terminal CLI. 34 tests green, no network needed.
Next: **M4 deployment to the VPS** (needs the owner's machine — see plan).

## Development

```bash
uv sync --extra dev    # gemini-router is a private git dep → GitHub auth required
                       # (GH_PAT_READ secret in CI; locally: git insteadOf with a PAT)
uv run pytest -q       # 34 tests: chunker, pipeline, tools/server, logging — all faked
uv run ruff check src tests
GEMINI_API_KEYS=... uv run gemini-mcp quota|calls|logs|prune|doctor
```

| Stage | Artifact |
|---|---|
| 1 · Unpacking/reframing + Q&A | [`docs/01-unpacking-reframing.md`](docs/01-unpacking-reframing.md) |
| 2 · Functional/business requirements | [`docs/02-requirements.md`](docs/02-requirements.md) |
| 3 · Architecture & stack | [`docs/03-architecture.md`](docs/03-architecture.md) |
| 4 · Plan (M0–M6) | [`docs/04-plan.md`](docs/04-plan.md) |
| Risks | [`docs/blockers/`](docs/blockers/) |

## Planned usage (Freebuff, `~/.agents/mcp.json`)

```jsonc
{ "mcpServers": {
    "gemini": {                       // A) SSH-stdio — primary, encrypted, no open ports
      "command": "ssh",
      "args": ["agent@YOUR_VPS_HOST", "/opt/apps/gemini-mcp/.venv/bin/gemini-mcp", "stdio"] },
    "gemini-http": {                  // B) HTTP via `ssh -L 8790:127.0.0.1:8790 agent@YOUR_VPS_HOST`
      "type": "http", "url": "http://127.0.0.1:8790/mcp",
      "headers": { "Authorization": "Bearer $GEMINI_MCP_TOKEN" } } } }
```

Tools: `analyze_large` (chunking pipeline), `gemini_generate` (single shot),
`quota_status`, `router_logs`.

## Deployment (M4)

From the workstation (sibling checkout of `gemini-router` required next to this repo):

```bash
bash scripts/run_remote.sh setup    # rsync both repos → venv, .env, systemd unit, doctor
bash scripts/run_remote.sh smoke    # first real-key run on the VPS (spends ≤1 RPD)
bash scripts/run_remote.sh status|logs
```

Freebuff Desktop wiring (SSH-stdio and HTTP-via-tunnel snippets, verification steps):
[docs/freebuff-wiring.md](docs/freebuff-wiring.md). The daemon never leaves
`127.0.0.1:8790`; no firewall changes are made (deployment notes compliant).

## Ops (after M4)

```bash
ssh agent@YOUR_VPS_HOST
sudo systemctl status gemini-mcp          # daemon (HTTP)
/opt/apps/gemini-mcp/.venv/bin/gemini-mcp quota    # what's left, per key×model
/opt/apps/gemini-mcp/.venv/bin/gemini-mcp logs -f  # live app log
```

Conventions: English in files, Conventional Commits, SemVer, docs updated with every
functional change (see `AGENTS.md`).
