# Freebuff Desktop ↔ gemini wiring

Two paths, both usable simultaneously (they share ONE ledger — quota spend is visible
together). Edit **home-level** `~/.agents/mcp.json` (project-level configs are cached
unreliably — Freebuff bug #957); fully restart Freebuff after editing.

## A) SSH-stdio (primary — zero extra moving parts)

```jsonc
{ "mcpServers": {
    "gemini": {
      "command": "ssh",
      "args": ["root@<YOUR_VPS_HOST>",
               "~/.local/bin/gemini", "stdio"]
    } } }
```

Requirements: key-based SSH from the workstation (`ssh-copy-id root@<YOUR_VPS_HOST>`).
No token needed — the transport is SSH. Each Freebuff session spawns its own process;
all sessions share `/opt/apps/shared/gemini-router/state.db`, so RPD/RPM accounting
stays global. The process bootstraps itself: it anchors to the project
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
    -o ExitOnForwardFailure=yes -L 8790:127.0.0.1:8790 root@<YOUR_VPS_HOST>
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

Rotation: `bash scripts/rotate_token.sh` on the server (mints, restarts daemon,
prints once), then the pipe above + the python mcp.json rewrite. SSH hardening
(key-only root, no password brute-force surface, no firewall rules):
`bash scripts/harden_sshd.sh` — run from an OPEN ssh session.

## Verify (2 minutes)

1. In Freebuff, ask the agent: *"call quota_status on gemini"*. Expect a table with
   5 flash models × your key, `RPD left 20/20` (or 19/20 after smoke).
2. Ask for a tiny analysis: *"use analyze_large: task 'summarize', body 'Hello world'"*
   → single-shot path, watch progress messages (b03: record WHICH levels render —
   progress notifications / log lines / only the final `meta.progress_trace`).
3. On the server: `bash scripts/run_remote.sh logs` → JSONL audit row with
   `source=mcp`, and `journalctl -u gemini -n 20`.

**Long calls are timeout-safe.** Clients cut MCP calls after ~30–60s
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
| `file_upload_ticket` | reserve an upload: one-time signed URL + scp path |
| `analyze_file` | analyze an uploaded file: text is extracted/chunked, PDFs/images/audio go to the provider Files API |
| `quota_status` | "which model can take a request right now?" |
| `router_logs` | recent calls/failures from the server ledger |

**File ingest recipe (M4.5, client stays maximally light — D11).** Every compute
response starts with a one-line `report` field (route · thinking · model · spend ·
rough edges) — the agent must relay it to chat verbatim; it travels in the payload
because clients truncate tool descriptions.

```bash
# 1) agent: file_upload_ticket(filename="paper.rtf", size_bytes=$(stat -f%z paper.rtf))
# 2) ONE upload command (either channel):
curl -T paper.rtf '<upload_url from the ticket>'      # via :8790 tunnel, sig = auth
scp paper.rtf root@<vps>:/opt/apps/gemini-mcp/inbox/<upload_id>   # no tunnel needed
# 3) agent: analyze_file(task="…", upload_id="…")
```

Server-side, two paths. **Text documents** (pandoc: RTF/DOCX/ODT/EPUB/HTML → GitHub-flavored
Markdown; plain and csv as-is) are chunked like any other body (`meta.file.extractor =
pandoc/rtf→gfm`, `encoding = markdown`); if pandoc is ever missing, RTF falls back to the
built-in extractor (cp1251 `\'xx` runs, `\uNNNN`, junk groups skipped). **PDFs, images,
audio and video** are uploaded to the provider's Files API and read there: one file is one
request, never split, `meta.file.tokens` carries the measured cost, and
`meta.file.provider` the provider name/uri/expiry. Re-analyzing the same `upload_id` reuses
the uploaded file until it expires. Anything else (zip/gzip/unknown) fails fast with
`binary_file_needs_conversion` and ZERO quota spent; a PDF over `pdf_max_mb`/`pdf_max_pages`
is refused before any upload. Inbox retention: `keep_hours` (default 24) +
`total_cap_mb` (default 500); pruned on each ticket issuance, at the start of every
`analyze_file` and by `gemini prune`. An upload ticket is spent by its first PUT
(the blob survives a replay, the spent marker is what refuses it), so a failed upload
means a fresh ticket, never a retry of the old URL.

**Routes.** `route:"optimal"` (default) tries models in empirical
availability order (`routing.optimal_chain`: older models are less loaded on the
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
  Server-side hardening shipped anyway: `WWW-Authenticate: Bearer` on
  401, RFC 9728 metadata, JSON (never text/plain) on every `/.well-known/*` and OAuth
  rung — discovery can no longer crash any client's parser.
  Server-side hardening shipped anyway: `WWW-Authenticate: Bearer` on
  401, RFC 9728 metadata on `/.well-known/oauth-protected-resource[/mcp]`, and JSON
  (never text/plain) on every other `/.well-known/*` path — no client can crash on
  discovery anymore. Native `type: http` remains the right choice for well-behaved
  clients (curl/SDK scripts).
