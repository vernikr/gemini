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
Open the tunnel via the **launchd agent** — it restarts the tunnel automatically on
death, after sleep, and at login (manual tunnels kept dying silently and every death
looked like "MCP server failed"):

```bash
cp scripts/gemini-tunnel.plist ~/Library/LaunchAgents/com.vernikr.gemini-tunnel.plist
launchctl load ~/Library/LaunchAgents/com.vernikr.gemini-tunnel.plist
curl -sf http://127.0.0.1:8790/healthz && echo OK   # log: /tmp/gemini-tunnel.log
```

Manual fallback (single session, dies on sleep — avoid):

```bash
ssh -f -N -o ServerAliveInterval=30 -o ServerAliveCountMax=4 \
    -o ExitOnForwardFailure=yes -L 8790:127.0.0.1:8790 root@YOUR_VPS_HOST
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

**Freebuff**: either the `type: http` block above or the `mcp-remote` bridge from
"Known client quirks" works — all early failures were a mis-copied token, not header
dropping. Prefer the bridge until native http is re-verified end-to-end.

`GEMINI_MCP_TOKEN` lives on the server in `/opt/apps/gemini-mcp/.env`. **Never
hand-copy it** (a stray character caused days of 401s); pull it over SSH:

```bash
ssh root@<vps> 'grep GEMINI_MCP_TOKEN /opt/apps/gemini-mcp/.env | cut -d= -f2 | tr -d "\n"' | pbcopy
```

## Verify (2 minutes)

1. In Freebuff, ask the agent: *"call quota_status on gemini"*. Expect a table with
   5 flash models × your key, `RPD left 20/20` (or 19/20 after smoke).
2. Ask for a tiny analysis: *"use analyze_large: task 'summarize', body 'Hello world'"*
   → single-shot path, watch progress messages (b03: record WHICH levels render —
   progress notifications / log lines / only the final `meta.progress_trace`).
3. On the server: `bash scripts/run_remote.sh logs` → JSONL audit row with
   `source=mcp`, and `journalctl -u gemini-mcp -n 20`.

**Long calls are timeout-safe (v0.3.0).** Clients cut MCP calls after ~30–60s
(Freebuff did exactly that to a thinking-heavy `analyze_large`), so both compute tools
hold the call inline only `wait_seconds` (default 25). A longer job returns
`{status:"running", request_id}` and keeps computing server-side; the agent then calls
`analyze_result(request_id)` (each call waits up to its own `wait_seconds`) until the
final `{result, meta}` arrives. After a daemon restart the job registry is empty →
`analyze_result` returns `{status:"unknown"}` and the original call should be repeated
with the SAME `request_id`: finished chunks/consolidations come from the router cache,
so the retry is nearly free. Audit gains `event:"async_submitted"` rows; job lifecycle
(submit/attach/done/fail/ticket) goes to the app log.

## Tools cheat-sheet (agent-facing)

| tool | when |
|---|---|
| `analyze_large` | any massive body; chunks + consolidates automatically |
| `gemini_generate` | direct single call (model pinning, thinking_level, json out) |
| `analyze_result` | fetch/await a background job that returned `status:"running"` |
| `quota_status` | "which model can take a request right now?" |
| `router_logs` | recent calls/failures from the server ledger |

**Routes (v0.4.0).** `route:"optimal"` (default) tries models in empirical
availability order 3.6 → 3.5 → 3-preview → 3.8 → 3.7 (older = less loaded on the
free tier) and auto-overflows to flash-lite/gemma when Flash budgets run out.
`route:"best_available"` is the legacy newest-first chain with opt-in overflow
(hard stop). `thinking_level` default is `medium` (per-request override wins).
Server instructions REQUIRE the agent to report in chat after every compute call:
route + thinking level used, the final model (`meta.model_final`/`meta.model`) and
rough edges from `meta.progress_trace` — 503s/retries, fallbacks, minute-limit
waits, failed chunks.

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
