# 06 · Consumer guide (no server needed)

gemini-mcp in **direct mode**: your machine → Google AI Studio free tier. No VPS, no
router, no daemon — one wizard run, then paste a JSON block into your MCP client.

## 1. Install & setup (once)

Any of these runs the same core (`gemini-mcp-gateway` on PyPI):

```bash
pnpm dlx @vernikr/gemini-mcp setup      # pnpm-first (npm shim; uv bootstrapped via npx)
npx -y @vernikr/gemini-mcp setup        # npm/npx equivalent
uvx --from gemini-mcp-gateway gemini-mcp setup   # pure Python toolchain
```

The wizard: fleet-vs-simple question (pick **1 Simple**) → masked key paste
(get keys free at https://aistudio.google.com/app/apikey, several keys = several times
the quota) → live listModels probe → writes `~/.config/gemini-mcp/`:

| File | Contents |
|---|---|
| `.env` (0600) | `GEMINI_API_KEYS=…` — the only place keys live |
| `config.yaml` | `backend: direct` + `logging.dir` under the same home |
| `state.db` | local usage ledger (created on first call; survives restarts) |

Non-interactive: `… setup --backend direct --keys 'AIza…,AIza…' --json`.
Undo everything: `… uninstall --yes`.

## 2. Add to your MCP client (JSON)

`setup` **prints** the JSON block but never edits client configs itself (formats differ
per client; auto-write is planned as M7.5). Since 0.9.1 the printout is mode-aware:
direct mode says **paste NOW** and shows the full `{"mcpServers": …}` wrapper (npx form
when `npx` is on PATH, uvx-pinned otherwise, plus where each client's config lives);
fleet/router mode says **do NOT paste yet** and lists the concrete server-deploy steps
first, then the ssh snippet. Two wiring forms:

- **After the wizard** (option A): keys already live in `~/.config/gemini-mcp/.env` —
  the block below needs no env.
- **Without the wizard** (option B, JSON-only): add `"env": {"GEMINI_API_KEYS":
  "AIza…key1,AIza…key2"}` to the server entry — that alone is sufficient; backend
  auto-resolves to `direct`, logs+ledger still land in `~/.config/gemini-mcp/`
  (bare-launch default). Trade-off: keys sit in the client config file, not a 0600 .env.
  More private JSON-only form — point at a keys FILE instead of inlining secrets:
  `"env": {"GEMINI_MCP_KEYS_FILE": "/absolute/path/keys.txt"}`.

### Key sources (priority high → low)

| Source | Example |
|---|---|
| CLI argument | `gemini-mcp --keys 'AIza…' stdio` — visible in `ps`/history, last resort |
| CLI file | `gemini-mcp --keys-file ~/keys.txt stdio` |
| env → file | `GEMINI_MCP_KEYS_FILE=/abs/path/keys.txt` (client-JSON `env` friendly) |
| config.yaml | `router:` → `keys_file: ~/keys.txt` |
| env inline | `GEMINI_API_KEYS=AIza…,AIza…` (`setup`'s `.env`, or client-JSON `env`) |

Keys-file format: comma/whitespace/newline separated, `#` comments ignored, deduped.
`setup` also accepts `--keys-file` (merged with `--keys`).

The gateway auto-discovers `~/.config/gemini-mcp/` — **no other env vars needed**.

### Freebuff Desktop — `~/.agents/mcp.json`

```json
{
  "mcpServers": {
    "gemini": {
      "command": "npx",
      "args": ["-y", "@vernikr/gemini-mcp", "stdio"]
    }
  }
}
```

### Claude Desktop — `claude_desktop_config.json`

(macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "gemini": {
      "command": "npx",
      "args": ["-y", "@vernikr/gemini-mcp", "stdio"]
    }
  }
}
```

### Cursor — `~/.cursor/mcp.json` (or Settings → MCP)

```json
{
  "mcpServers": {
    "gemini": {
      "command": "npx",
      "args": ["-y", "@vernikr/gemini-mcp", "stdio"]
    }
  }
}
```

### Generic / any stdio client — uvx form (version-pinned, what `setup` prints)

```json
{
  "mcpServers": {
    "gemini": {
      "command": "uvx",
      "args": ["--from", "gemini-mcp-gateway==0.8.1", "gemini-mcp", "stdio"]
    }
  }
}
```

Restart the client fully after editing (configs are read at startup). First call pays a
one-time uv resolve (~10–20 s), then it's cached.

### Fleet mode instead (you DO have a server)

```json
{
  "mcpServers": {
    "gemini": {
      "command": "ssh",
      "args": ["root@<YOUR_VPS>", "/opt/apps/gemini-mcp/.venv/bin/gemini-mcp", "stdio"]
    }
  }
}
```

HTTP-tunnel variant (`ssh -L 8790:127.0.0.1:8790 root@<YOUR_VPS>` running):

```json
{
  "mcpServers": {
    "gemini-http": {
      "type": "http",
      "url": "http://127.0.0.1:8790/mcp",
      "headers": { "Authorization": "Bearer $GEMINI_MCP_TOKEN" }
    }
  }
}
```

## 3. Use it

Ask the agent (short-phrase template):

> через gemini разбери файл <путь>, задача: <словами>

Tools: `analyze_large` (chunked massive-body analysis), `gemini_generate` (single
shot), `analyze_file` (upload ticket + server-side RTF/DOCX extraction),
`analyze_result` (async ticket poll), `quota_status`, `router_logs`. Every compute
answer starts with a one-line `report` (`backend=direct · route=… · model=… · req=N ·
…s · clean run`) — the agent should relay it.

Terminal (no client needed):

```bash
npx -y @vernikr/gemini-mcp doctor    # keys, chain, limits, live probe, local ledger
npx -y @vernikr/gemini-mcp quota     # per key×model budget table (live, local)
npx -y @vernikr/gemini-mcp calls -n 20   # local audit tail
```

## 4. Free-tier facts & troubleshooting

- Limits: 20 RPD / 5 RPM / 250k TPM **per key×model**; quota day resets at midnight
  **America/Los_Angeles**; the chain `3.6 → 3.5 → 3-preview → 3.8 → 3.7-flash` walks
  models automatically, `quota_exhausted` errors carry the reset ETA + overflow hint.
- Direct-mode counters are per-machine (local `state.db`): accurate across restarts and
  concurrent local clients, NOT shared with other machines — that's fleet mode's job.
- `ETARGET`/404 right after a release: npm registry propagation takes ~2–4 min — retry.
- Empty `healthz` through a tunnel (fleet): the tunnel isn't loaded —
  `launchctl load ~/Library/LaunchAgents/com.vernikr.gemini-tunnel.plist`
  (`Load failed: 5` there means it's ALREADY loaded — verify with curl instead).
- Problems: `doctor` first; deeper: `docs/08-dual-mode.md` (modes),
  `docs/05-packaging.md` (install internals), `docs/blockers/` (known risks).
