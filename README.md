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

**Planning complete (stages 1–4). Development starts at M0 — see the plan.**

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

## Ops (after M4)

```bash
ssh agent@YOUR_VPS_HOST
sudo systemctl status gemini-mcp          # daemon (HTTP)
/opt/apps/gemini-mcp/.venv/bin/gemini-mcp quota    # what's left, per key×model
/opt/apps/gemini-mcp/.venv/bin/gemini-mcp logs -f  # live app log
```

Conventions: English in files, Conventional Commits, SemVer, docs updated with every
functional change (see `AGENTS.md`).
