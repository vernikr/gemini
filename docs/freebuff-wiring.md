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

- **"Invalid OAuth error response … Raw body: Not Found"** when enabling `gemini-http`
  (Freebuff, pre-v0.2.2): the client probes `/mcp` unauthenticated, gets 401, then runs
  OAuth discovery on `/.well-known/oauth-protected-resource`; the default text/plain 404
  crashed its JSON parser. v0.2.2 serves RFC 9728 metadata there (static bearer, no
  authorization server) and adds `WWW-Authenticate: Bearer` to 401s. Fix: `git pull` +
  `run_remote.sh setup`, then FULLY restart Freebuff.
- Fallback bridge (if a client still can't do header auth): run it through `mcp-remote`,
  the same pattern Freebuff already uses for Atlassian/Apify:
  `{"command":"npx","args":["-y","mcp-remote","http://127.0.0.1:8790/mcp","--header",
  "Authorization: Bearer <TOKEN>"]}`.
