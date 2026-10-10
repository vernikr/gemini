# @vernikr/gemini-mcp

Gemini **free-tier** MCP gateway that needs **no server**: your MCP client (Freebuff,
Claude Desktop, Cursor, …) talks to Google AI Studio through a quota-aware router —
cascading Flash chain, per key×model RPD/RPM/TPM budgeting, minute-limit waiting,
massive-body chunked analysis with consolidation. Python core `gemini-mcp-gateway`
(PyPI), bootstrapped via [uv](https://docs.astral.sh/uv/) — no system Python needed.

## Get API keys (free)

<https://aistudio.google.com/app/apikey> — several keys = several times the quota
(20 requests/day per key×model).

## Quick start

### If you're human

```bash
pnpm dlx @vernikr/gemini-mcp setup
```

Answer the questions (pick **1 Simple** unless you run a server; keys: hidden paste,
a file, or skip). The wizard finishes with a short numbered checklist — paste one
JSON block into your MCP client (or let it write that for you: add
`--write-client freebuff|claude|cursor`), restart the client, send your agent the
printed verify prompt. Fleet (pick **2**): it also deploys your VPS end-to-end
(clone → provision → keys → healthcheck) and prints the final block with your real
host already inside.

### If you're an AI agent

One shot, no TTY, machine-readable. Exit code `0` + `"ok": true` = done; otherwise
every failure carries `error`/`deploy_error`/`clients_failed[].fix` naming the exact
remedy — apply it and re-run the same command (idempotent).

```bash
# direct (no server); keys from a file — never from shell history
npx -y @vernikr/gemini-mcp setup --backend direct \
  --keys-file /abs/path/keys.txt --write-client freebuff --json

# fleet (shared-quota server); clones/provisions/health-checks by itself
npx -y @vernikr/gemini-mcp setup --backend router --vps-host <ip> \
  [--vps-user root] [--projects-dir ~/projects] [--keys-file /abs/path/keys.txt] \
  --write-client freebuff --json
```

Fleet prerequisites you must verify first (each failure message tells you how):
`git ls-remote https://github.com/vernikr/gemini-mcp` succeeds (private repos —
GitHub auth), `ssh <user>@<ip> 'echo ok'` succeeds (key auth), bash+rsync present,
macOS/Linux. What is left for the human — relay exactly these two manual actions
from the JSON: **restart the MCP client fully**, then **send the agent the prompt in
`verify_prompt`** (tool `quota_status`, MCP server `gemini`).

Analyzing a FILE (agent): never read it yourself and never inline its contents into a
prompt. Call `file_upload_ticket(filename, size_bytes)` → HTTP PUT the raw bytes to
`upload_url` → `analyze_file(upload_id, task)`. Extraction (pandoc), chunking and quota
are server-side; `quota_status` repeats this recipe in its `file_flow` field, and raw
RTF/PDF/DOCX payloads in text tools fail fast with a `raw_document_inlined` fix.

## Wiring — 2 options

### A · Wizard (recommended): `setup`, then paste one JSON block

```bash
pnpm dlx @vernikr/gemini-mcp setup
```

What it does — and what it does NOT:

- asks one question (simple/direct vs fleet/router; pick **1** unless you run a server);
- takes keys via a **masked** paste, a file path, or skips — stored in
  `~/.config/gemini-mcp/.env` (mode `0600` — the only place keys live), plus a minimal
  `config.yaml`;
- fleet (pick **2**): asks for your VPS host and projects folder, then does the server
  side itself — clone/update repos, provision, push keys, healthcheck — and ends with
  the same paste-once JSON (`--vps-host`, `--vps-user`, `--projects-dir` skip prompts);
- probes keys live (listModels — zero quota spent) and prints fingerprints, never keys;
- **prints** the ready `mcpServers` JSON for your client — paste it once, or pass
  `--write-client freebuff|claude_desktop|cursor` and the wizard merges the `gemini`
  entry into the client's config file itself (`.bak` backup kept).

After the wizard the client JSON needs no env/keys — the gateway auto-discovers
`~/.config/gemini-mcp/`:

```json
{
  "mcpServers": {
    "gemini": { "command": "npx", "args": ["-y", "@vernikr/gemini-mcp", "stdio"] }
  }
}
```

### B · JSON-only (skip the wizard entirely)

Is pasting JSON into `mcp.json` enough? **Yes — if the keys travel with it** (without
keys anywhere the gateway fails fast by design). Inline in `env`:

```json
{
  "mcpServers": {
    "gemini": {
      "command": "npx",
      "args": ["-y", "@vernikr/gemini-mcp", "stdio"],
      "env": { "GEMINI_API_KEYS": "AIza…key1,AIza…key2" }
    }
  }
}
```

…or — more private, secrets stay out of the client config — point at a keys **file**
(comma/whitespace/newline separated, `#` comments ok):

```json
{ "env": { "GEMINI_MCP_KEYS_FILE": "/absolute/path/keys.txt" } }
```

Key sources, priority high → low: `--keys` / `--keys-file` CLI flags (any command,
before the subcommand) → `GEMINI_MCP_KEYS_FILE` → `router.keys_file` in config.yaml →
`GEMINI_API_KEYS` env (this is what the wizard's `.env` provides).

Backend auto-resolves to `direct`; logs and the local usage ledger (`state.db` —
quota counters survive restarts) go to `~/.config/gemini-mcp/`. Trade-off vs option A:
keys sit in the client config file instead of a `0600` `.env`.

Where to paste (restart the client fully afterwards): Freebuff `~/.agents/mcp.json` ·
Claude Desktop `~/Library/Application Support/Claude/claude_desktop_config.json` ·
Cursor `~/.cursor/mcp.json`. First launch pays a one-time uv resolve (~10–20 s).

## Commands

```bash
pnpm dlx @vernikr/gemini-mcp doctor          # keys, chain, limits, live probe, ledger
pnpm dlx @vernikr/gemini-mcp quota           # per key×model budget table (live)
pnpm dlx @vernikr/gemini-mcp calls -n 20     # local audit tail
pnpm dlx @vernikr/gemini-mcp uninstall --yes # remove keys, config and ledger
```

All commands accept `--json` (agent-facing output). MCP tools: `analyze_large`,
`gemini_generate`, `analyze_file` (server-side RTF/DOCX extraction), `analyze_result`,
`quota_status`, `router_logs`. Every compute answer starts with a one-line `report`
(`backend=direct · route=… · model=… · req=N · …s · clean run`).

## How the shim works

`bin/gemini-mcp.js` is a ~30-line bootstrap: it runs the version-pinned Python core
(`uvx --from gemini-mcp-gateway==<same version> gemini-mcp …`), self-installing uv
through the official astral.sh installer when needed (uv is not distributed via npm).
Zero logic in JS; npm ↔ PyPI versions are
lockstep. Fleet mode (shared quota ledger on your own server) and full docs:
[github.com/vernikr/gemini-mcp](https://github.com/vernikr/gemini-mcp) —
`docs/06-consumer.md`, `docs/08-dual-mode.md`.
