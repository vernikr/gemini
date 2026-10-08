# Freebuff Desktop ↔ gemini-mcp wiring

Two paths, both usable simultaneously (they share ONE ledger — quota spend is visible
together). Edit **home-level** `~/.agents/mcp.json` (project-level configs are cached
unreliably — Freebuff bug #957); fully restart Freebuff after editing.

## A) SSH-stdio (primary — zero extra moving parts)

```jsonc
{ "mcpServers": {
    "gemini": {
      "command": "ssh",
      "args": ["root@YOUR_VPS_HOST",
               "/opt/apps/gemini-mcp/.venv/bin/gemini-mcp", "stdio"]
    } } }
```

Requirements: key-based SSH from the workstation (`ssh-copy-id root@YOUR_VPS_HOST`).
No token needed — the transport is SSH. Each Freebuff session spawns its own process;
all sessions share `/opt/apps/shared/gemini-router/state.db`, so RPD/RPM accounting
stays global. Since v0.2.1 the process bootstraps itself: it anchors to the project
root (`/opt/apps/gemini-mcp`) and loads its `.env` (keys, state path) — no wrapper
commands or env plumbing in the MCP config needed.

## B) HTTP daemon via SSH tunnel (for other clients / multiple agents)

The daemon listens on `127.0.0.1:8790` only (never exposed publicly — blocker b06).
Open a tunnel first, keep it running:

```bash
ssh -N -L 8790:127.0.0.1:8790 root@YOUR_VPS_HOST &
```

Then in `~/.agents/mcp.json`:

```jsonc
{ "mcpServers": {
    "gemini-http": {
      "type": "http",
      "url": "http://127.0.0.1:8790/mcp",
      "headers": { "Authorization": "Bearer $GEMINI_MCP_TOKEN" }
    } } }
```

Keep the tunnel alive across idle periods (and fail loudly instead of dying silently):

```bash
ssh -f -N -o ServerAliveInterval=30 -o ServerAliveCountMax=4 \
    -o ExitOnForwardFailure=yes -L 8790:127.0.0.1:8790 root@YOUR_VPS_HOST &
```

**Freebuff specifically**: its native http client drops configured headers — use the
`mcp-remote` bridge from "Known client quirks" instead of the block above.

`GEMINI_MCP_TOKEN` must be exported in the environment Freebuff launches from; the
value lives on the server in `/opt/apps/gemini-mcp/.env` (`sudo grep TOKEN
/opt/apps/gemini-mcp/.env`). If `$VAR` expansion misbehaves in your build, paste the
token literally (bug #957 thread shows both forms working).

## Verify (2 minutes)

1. In Freebuff, ask the agent: *"call quota_status on gemini"*. Expect a table with
   5 flash models × your key, `RPD left 20/20` (or 19/20 after smoke).
2. Ask for a tiny analysis: *"use analyze_large: task 'summarize', body 'Hello world'"*
   → single-shot path, watch progress messages (b03: record WHICH levels render —
   progress notifications / log lines / only the final `meta.progress_trace`).
3. On the server: `bash scripts/run_remote.sh logs` → JSONL audit row with
   `source=mcp`, and `journalctl -u gemini-mcp -n 20`.

## Tools cheat-sheet (agent-facing)

| tool | when |
|---|---|
| `analyze_large` | any massive body; chunks + consolidates automatically |
| `gemini_generate` | direct single call (model pinning, thinking_level, json out) |
| `quota_status` | "which model can take a request right now?" |
| `router_logs` | recent calls/failures from the server ledger |

## Known client quirks (as-built)

- **All early HTTP-path failures (401 → OAuth dance → discovery crashes) traced to ONE
  root cause (2026-10-08): a hand-copied bearer token with a stray character (65 chars
  instead of 64).** Freebuff sent the configured headers correctly all along; the
  server rightfully 401'd and spec-compliant clients fell into the OAuth ladder.
  ⇒ Never hand-copy the token: pull it over SSH (`ssh root@<vps> 'grep GEMINI_MCP_TOKEN
  /opt/apps/gemini-mcp/.env | cut -d= -f2 | tr -d "\n"' | pbcopy) or rewrite mcp.json
  programmatically. The `mcp-remote` bridge remains the recommended Freebuff entry
  (proven in sandbox); native `type: http` is equally fine once the token is exact.
  Server-side hardening shipped anyway (v0.2.2–v0.2.4): `WWW-Authenticate: Bearer` on
  401, RFC 9728 metadata, JSON (never text/plain) on every `/.well-known/*` and OAuth
  rung — discovery can no longer crash any client's parser.
  Server-side hardening shipped anyway (v0.2.2/v0.2.3): `WWW-Authenticate: Bearer` on
  401, RFC 9728 metadata on `/.well-known/oauth-protected-resource[/mcp]`, and JSON
  (never text/plain) on every other `/.well-known/*` path — no client can crash on
  discovery anymore. Native `type: http` remains the right choice for well-behaved
  clients (curl/SDK scripts).
