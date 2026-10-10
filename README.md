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
- **Dual backend (0.8.0)**: `router` (fleet: shared SQLite ledger on the VPS) or
  `direct` (plain Google AI Studio: same engine, in-memory counters, no server at all).
  `backend: auto` picks router only when persistent state exists; env
  `GEMINI_MCP_BACKEND` overrides. See [`docs/08-dual-mode.md`](docs/08-dual-mode.md).

## Quick start

### If you're human

```bash
pnpm dlx @vernikr/gemini-mcp setup     # or: uvx --from gemini-mcp-gateway gemini-mcp setup
```

Answer the questions: **1 Simple** (direct to Google AI Studio, keys via hidden
paste/file/skip) or **2 Fleet** (your VPS — the wizard clones the repos, provisions
the server, pushes keys, health-checks, all you answer is host + projects folder).
It finishes with a numbered checklist: paste one JSON block into your MCP client
(Freebuff/Claude Desktop/Cursor — or add `--write-client freebuff|claude|cursor`
and it merges the block in for you, with a `.bak` backup), restart the client,
send the agent the printed verify prompt. `gemini-mcp doctor` re-checks
keys/chain/limits; `gemini-mcp uninstall --yes` removes everything the wizard wrote.
Full client matrix + fleet JSON forms: [`docs/06-consumer.md`](docs/06-consumer.md).

### If you're an AI agent

One shot, no TTY, machine-readable. Exit `0` + `"ok": true` = done; otherwise every
failure carries `error`/`deploy_error`/`clients_failed[].fix` naming the exact remedy —
apply it, re-run the same command (idempotent). Relay to the human only the two manual
actions: restart the MCP client, then send the agent the prompt from `verify_prompt`
(tool `quota_status`, MCP server `gemini`).

```bash
# direct (no server); keys from a file — never from shell history
npx -y @vernikr/gemini-mcp setup --backend direct \
  --keys-file /abs/path/keys.txt --write-client freebuff --json

# fleet (shared-quota server); clones/provisions/health-checks by itself
npx -y @vernikr/gemini-mcp setup --backend router --vps-host <ip> \
  [--vps-user root] [--projects-dir ~/projects] [--keys-file /abs/path/keys.txt] \
  --write-client freebuff --json
```

Fleet prerequisites (each failure message tells you how to check): GitHub auth for
the private repos (`git ls-remote https://github.com/vernikr/gemini-mcp`), passwordless
`ssh <user>@<ip>`, bash+rsync, macOS/Linux. No env vars needed afterwards: the gateway
auto-discovers `~/.config/gemini-mcp/`.

Analyzing a FILE (agent): never read it yourself and never inline its contents into a
prompt. Call `file_upload_ticket(filename, size_bytes)` → HTTP PUT the raw bytes to
`upload_url` → `analyze_file(upload_id, task)`. Extraction, chunking and quota are
server-side; `quota_status` returns this recipe in its `file_flow` field, and raw
RTF/PDF/DOCX payloads in text tools fail fast with a `raw_document_inlined` fix.

Direct mode needs nothing but API keys. Usage is tracked in a LOCAL persistent ledger
(`~/.config/gemini-mcp/state.db`): quota counters survive restarts and are shared by all
gateway processes on the machine — but not across machines (that's fleet mode: VPS
daemon + `backend: router`). Every response says `backend=direct` in its `report` line;
`gemini-mcp uninstall --yes` removes keys, config and ledger together. The npm shim
`@vernikr/gemini-mcp` (pnpm-first install) ships the same core at the same version.

## Status

**v0.8.0 — published consumer package.** Fleet side (VPS daemon 0.6.x, SSH-stdio +
HTTP tunnel) runs in production; consumer side ships as PyPI `gemini-mcp-gateway`
(+ npm shim `@vernikr/gemini-mcp`) with the dual-mode backend (`router`/`direct`,
docs/08) and the `setup` wizard (M7.2). Router core: `gemini-router` 0.1.1 on PyPI.
80 tests green, no network needed (respx/fakes only).
Next: M7.3 dual-publish CI, M7.4 clean-machine beta.

## Development

```bash
uv sync --extra dev    # dev pin: gemini-router from git tag (tool.uv.sources);
                       # published metadata uses the PyPI version range — no auth needed
uv run pytest -q       # 80 tests: chunker, pipeline, tools/server, logging, direct
                       # backend (respx), setup wizard — all faked, no network
uv run ruff check src tests
GEMINI_API_KEYS=... uv run gemini-mcp setup|quota|calls|logs|prune|doctor
```

| Stage | Artifact |
|---|---|
| 1 · Unpacking/reframing + Q&A | [`docs/01-unpacking-reframing.md`](docs/01-unpacking-reframing.md) |
| 2 · Functional/business requirements | [`docs/02-requirements.md`](docs/02-requirements.md) |
| 3 · Architecture & stack | [`docs/03-architecture.md`](docs/03-architecture.md) |
| 4 · Plan (M0–M6) | [`docs/04-plan.md`](docs/04-plan.md) |
| Consumer guide (client JSON, direct mode) | [`docs/06-consumer.md`](docs/06-consumer.md) |
| Dual-mode backend design | [`docs/08-dual-mode.md`](docs/08-dual-mode.md) |
| Risks | [`docs/blockers/`](docs/blockers/) |

## Planned usage (Freebuff, `~/.agents/mcp.json`)

```jsonc
{ "mcpServers": {
    "gemini": {                       // A) SSH-stdio — primary, encrypted, no open ports
      "command": "ssh",
      "args": ["root@YOUR_VPS_HOST", "/opt/apps/gemini-mcp/.venv/bin/gemini-mcp", "stdio"] },
    "gemini-http": {                  // B) HTTP via `ssh -L 8790:127.0.0.1:8790 root@YOUR_VPS_HOST`
      "type": "http", "url": "http://127.0.0.1:8790/mcp",
      "headers": { "Authorization": "Bearer $GEMINI_MCP_TOKEN" } } } }
```

Tools: `analyze_large` (chunking pipeline), `gemini_generate` (single shot),
`file_upload_ticket` + `analyze_file` (file flow: upload → server-side pandoc
extraction → analysis), `analyze_result` (async poll), `quota_status`, `router_logs`.
Raw document bytes (RTF/PDF/DOCX markers) pasted into `gemini_generate`/`analyze_large`
are rejected with `raw_document_inlined` + the exact fix — always use the file flow.

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
ssh root@YOUR_VPS_HOST
sudo systemctl status gemini-mcp          # daemon (HTTP)
/opt/apps/gemini-mcp/.venv/bin/gemini-mcp quota    # what's left, per key×model
/opt/apps/gemini-mcp/.venv/bin/gemini-mcp logs -f  # live app log
```

Conventions: English in files, Conventional Commits, SemVer, docs updated with every
functional change (see `AGENTS.md`).
