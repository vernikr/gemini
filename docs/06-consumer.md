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

The wizard: fleet-vs-simple question (pick **1 Simple**) → key intake — masked paste,
read from a file, or skip. The hidden-paste path is guarded because toggling tty echo
can crash macOS Terminal.app; the file option never touches tty modes. Get keys free at
https://aistudio.google.com/app/apikey — several keys = several times the quota → live listModels probe → writes `~/.config/gemini-mcp/`:

| File | Contents |
|---|---|
| `.env` (0600) | `GEMINI_API_KEYS=…` — the only place keys live |
| `config.yaml` | `backend: direct` + `logging.dir` under the same home |
| `state.db` | local usage ledger (created on first call; survives restarts) |

Non-interactive: `… setup --backend direct --keys-file keys.txt --write-client
freebuff --json` (machine-readable report: `ok`, `steps`, `verify_prompt`, and every
failure with a `fix` — built for AI agents; see README §Quick start).
Undo everything: `… uninstall --yes`.

## 2. Add to your MCP client (JSON)

`setup` **prints** the JSON block and can also **write** it itself:
`--write-client freebuff|claude|cursor` (repeatable) merges the `gemini` entry into
that client's config with a timestamped `.bak` backup; a broken existing JSON is
never clobbered — you get a `fix` instead. The printout is mode-aware: direct mode
and a successfully deployed fleet both end with the same paste-now block and a
numbered checklist (`steps` + highlighted `verify_prompt` in the report); only if the
fleet auto-deploy can't run (missing host, no git/ssh auth, Windows) it falls back to
short numbered manual steps. Two wiring forms:

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
      "args": ["--from", "gemini-mcp-gateway", "gemini-mcp", "stdio"]
    }
  }
}
```

Restart the client fully after editing (configs are read at startup). First call pays a
one-time uv resolve (~10–20 s), then it's cached.

### Fleet mode instead (you DO have a server)

The wizard does the whole server side. Answer its questions (or pass flags):

```bash
gemini-mcp setup --backend router --vps-host <ip> \
  [--vps-user root] [--projects-dir ~/projects] [--keys 'AIza…' | --keys-file f]
```

It clones/updates `vernikr/gemini-mcp` + `vernikr/gemini-router` into the projects
folder (existing clones are `git pull`ed — never re-cloned over), runs
`scripts/run_remote.sh setup` (rsync + provision + systemd unit, output streamed),
optionally writes your Gemini keys into the server's `.env` (0600, keys travel over
stdin — never visible in `ps`/history), waits for `/healthz`, and prints the final
paste-NOW snippet with your real host. Requirements: macOS/Linux, git authenticated
to GitHub (private repos — `gh auth login` or a credential helper), ssh access to the
server. After any `git tag`, push-CD updates the server automatically (docs/09-ci.md).

The snippet it prints (host already filled in):

```json
{
  "mcpServers": {
    "gemini": {
      "command": "ssh",
      "args": ["root@<YOUR_VPS>", "/root/.local/bin/gemini-mcp", "stdio"]
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
shot), `analyze_file` (upload ticket + routing: text documents are extracted and chunked,
PDFs/images/audio are read by the provider),
`analyze_result` (async ticket poll), `quota_status`, `router_logs`. Every compute
answer starts with a one-line `report` (`backend=direct · route=… · model=… · req=N ·
…s · clean run`) — the agent should relay it.

File flow (for the agent — also returned by `quota_status` in the `file_flow` field):
1) `file_upload_ticket(filename, size_bytes)` → 2) HTTP PUT the raw bytes to
`upload_url` → 3) `analyze_file(upload_id, task)`. The server extracts clean text
(pandoc) and chunks it, or — for PDFs, images and audio — sends the file to the provider
once and asks one measured question about it; spending is accounted either way.
Pasting raw file contents into
`gemini_generate`/`analyze_large` is rejected (`raw_document_inlined`): markup like RTF
is unreadable for the model (non-ASCII text survives as hex escapes).

PDFs are never split into parts: one file is one request, and `meta.file.tokens` carries
the measured cost. `ingest.pdf_max_mb` / `pdf_max_pages` mirror the provider's document
ceilings — a file over them is refused before any upload, with the number that tripped.
A second `analyze_file` on the same `upload_id` reuses the uploaded file (the reference
lives in the inbox sidecar) until the provider expires it or the key changes — a repeat
question is cheap and fast. Timing measured on the production daemon: the provider reports
an upload ACTIVE in ~2 s, and a fresh file answers in 2–5 s at `thinking=low` and ~10 s at
the default level — the bulk of the wait is the model (default thinking spends 1000+
thinking tokens even on one-word questions), not file handling; the provider can also
answer 503 under demand spikes, which the router retries. Never read a slow answer as a
hang, and never resubmit: each generate call spends quota, while `count_file_tokens` — the
step that produces `meta.file.tokens` — spends none.

Per-upload cap: `ingest.max_file_mb`. For a text document it is a memory guard sized
against the service's `MemoryMax` — the pipeline holds the extracted text in RAM while
chunking, at roughly 4.6 MB of RSS per uploaded megabyte, so a 350 MB ceiling is reached
near 60 MB of Cyrillic text and sooner for Latin scripts. For a PDF, image or audio file
the blob is streamed to the provider and never held in RAM, so the cap there is about
transfer and disk, not memory — raising it for such a file (with the owner's word) risks
much less than for a document.

An agent can exceed the cap per call with `override_size_limit`, which the tool
instructions tell it to use only after the user has agreed in chat, having been told
the service may die mid-job. Every such ticket is logged to `app.log` with its size and
filename. There is no server-side switch — the user's consent is the gate.

Terminal (no client needed):

```bash
npx -y @vernikr/gemini-mcp doctor    # keys, chain, limits, live probe, local ledger
npx -y @vernikr/gemini-mcp quota     # per key×model budget table (live, local)
npx -y @vernikr/gemini-mcp calls -n 20   # local audit tail
```

## 4. Free-tier facts & troubleshooting

- Limits: 20 RPD / 5 RPM / 250k TPM **per key×model**; quota day resets at midnight
  **America/Los_Angeles**; the chain (`routing.optimal_chain`)
  walks models automatically, `quota_exhausted` errors carry the reset ETA + overflow hint.
- Direct-mode counters are per-machine (local `state.db`): accurate across restarts and
  concurrent local clients, NOT shared with other machines — that's fleet mode's job.
- `ETARGET`/404 right after a release: npm registry propagation takes ~2–4 min — retry.
- Empty `healthz` through a tunnel (fleet): the tunnel isn't loaded —
  `launchctl load ~/Library/LaunchAgents/com.vernikr.gemini-tunnel.plist`
  (`Load failed: 5` there means it's ALREADY loaded — verify with curl instead).
- Problems: `doctor` first; deeper: `docs/08-dual-mode.md` (modes),
  `docs/worklog/archive/05-packaging.md` (install internals),
  `docs/blockers/` (known risks).
