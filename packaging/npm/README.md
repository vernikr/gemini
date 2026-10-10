# @vernikr/gemini-mcp

Gemini **free-tier** MCP gateway that needs **no server**: your MCP client (Freebuff,
Claude Desktop, Cursor, …) talks to Google AI Studio through a quota-aware router —
cascading Flash chain, per key×model RPD/RPM/TPM budgeting, minute-limit waiting,
massive-body chunked analysis with consolidation. Python core `gemini-mcp-gateway`
(PyPI), bootstrapped via [uv](https://docs.astral.sh/uv/) — no system Python needed.

## Get API keys (free)

<https://aistudio.google.com/app/apikey> — several keys = several times the quota
(20 requests/day per key×model).

## Wiring — 2 options

### A · Wizard (recommended): `setup`, then paste one JSON block

```bash
pnpm dlx @vernikr/gemini-mcp setup
```

What it does — and what it does NOT:

- asks one question (simple/direct vs fleet/router; pick **1**);
- takes keys through a **masked** prompt and stores them in `~/.config/gemini-mcp/.env`
  (mode `0600` — the only place keys live), plus a minimal `config.yaml`;
- probes keys live (listModels — zero quota spent) and prints fingerprints, never keys;
- **prints** the ready `mcpServers` JSON for your client — it does **not** edit any
  client config itself (formats differ per client; you paste the block once).

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

Is pasting JSON into `mcp.json` enough? **Yes — if the keys travel in its `env`**
(without keys anywhere the gateway fails fast by design):

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
through `npx -y @astral-sh/uv` when needed. Zero logic in JS; npm ↔ PyPI versions are
lockstep. Fleet mode (shared quota ledger on your own server) and full docs:
[github.com/vernikr/gemini-mcp](https://github.com/vernikr/gemini-mcp) —
`docs/06-consumer.md`, `docs/08-dual-mode.md`.
