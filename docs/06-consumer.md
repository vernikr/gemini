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

### Growing or rotating the pool later

```bash
gemini-mcp keys                 # where the pool lives + fingerprints (never raw keys)
gemini-mcp keys add <key>…      # validate → live probe (listModels, free) → write 0600
gemini-mcp keys remove <n|fp>   # by index or fingerprint; the last key needs --force
```

The command finds the right file by itself — the systemd unit's `EnvironmentFile` on a
server, the wizard's `.env` on a workstation — and restarts the service when there is one
(`--no-restart` skips that).

On the VPS the command is on PATH for every shell (`/usr/local/bin/gemini-mcp`); when a
host says `command not found` while the tool is installed, that shim is the missing piece.

Both key generations work and neither needs a decision from you: classic `AIza…` (Cloud
Console) and newer `AQ.…` (AI Studio). The probe runs the same code the daemon runs, and
when a key turns out to need a different transport it says so (`ok (query)`) and keeps
that choice for the process. A key the provider has blocked as leaked is named as such,
with what to do about it, instead of a bare 403. Several keys = several times the daily allowance, because
quota is counted per key × model; the router spreads calls across the pool by remaining
budget and steps past an exhausted or rejected key on its own.

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
PDFs/images/audio are read by the provider), `analyze_files` (several uploaded files,
one answer), `analyze_result` (async ticket poll), `quota_status`, `router_logs`. Every
compute answer starts with a one-line `report` (`backend=direct · route=… · model=… ·
req=N · …s · clean run`) — the agent should relay it.

The one-line `report` shows EVERY distinct model used; an unrequested flash-lite or
gemma is shown as `⚠️ model-name` even if a stronger model made the final summary.
Relay the ⚠️ and its reason for any retry, wait, incomplete chunk, split PDF, or cost
drift. If the call failed entirely, prefix your explanation with ⛔️ and give the
recovery step. Neither sign belongs on a clean run.

While a job runs, each `{status:'running'}` ticket carries `live`: the current phase
(`generating`, `waiting_rpm`/`waiting_tpm`, `chunking`, `consolidating`, …), `eta_s`
while a quota gate is being waited out (counts down on every poll), the model, the key
id and the latest stage line — enough to tell the user "still generating, about 40s of
quota wait left" instead of "still running".

File flow (for the agent — also returned by `quota_status` in the `file_flow` field):
1) `file_upload_ticket(filename, size_bytes)` → 2) HTTP PUT the raw bytes to
`upload_url` → 3) `analyze_file(upload_id, task)`. The server extracts clean text
(pandoc) and chunks it, or — for PDFs, images and audio — sends the file to the provider
once and asks one measured question about it; spending is accounted either way.
Pasting raw file contents into
`gemini_generate`/`analyze_large` is rejected (`raw_document_inlined`): markup like RTF
is unreadable for the model (non-ASCII text survives as hex escapes).

Several files, one answer: repeat ticket + upload for each file, then call
`analyze_files(task, upload_ids=[…])`. Text documents keep the chunked pipeline each;
PDFs/images/audio are measured for free and packed by measured tokens into as few group
requests as fit the key's full minute budget, then everything consolidates into ONE
result (`meta.groups` lists the packs, `meta.files` the per-file facts). A PDF that
alone exceeds the full quota is split by page ranges and consolidated into ONE answer
for that file before any other files are merged; its chunks are NEVER mixed with other
files in a provider request. Other over-budget binaries refuse with their measured
cost. One `upload_id` behaves exactly like `analyze_file`.

PDFs fitting the full minute budget are not split merely because the window is
occupied — the router can wait for it. Oversized PDFs are cut server-side by qpdf;
`meta.file.split` or `meta.files[].split` lists page ranges and the resolution.
`meta.file.tokens` carries the original measured cost. Install `qpdf` on the machine
running gemini-mcp to enable page-range cutting (`sudo apt install qpdf` on Ubuntu,
`brew install qpdf` on macOS); `pdfinfo` (poppler) is required for the page count.
Without either binary an oversized PDF fails with an explicit ⛔️ recovery hint; files
that fit still work. Media is processed at `ingest.media_resolution` — default `high` (the
owner's call, 2026-10-11: the per-image cost is small next to the minute limits, and scans
keep their small print). The level is the price: per-level prices, the provider defaults
and their provenance live in ONE table — `gemini_router.tariffs.MEDIA_TARIFFS` in the
router library — and the default differs per media type, so `high` roughly doubles a PDF
page against the default. Set `""` to inherit the provider default instead;
`meta.file.resolution` reports what was used. `ingest.pdf_max_mb` / `pdf_max_pages` mirror
the provider's document ceilings — a file over them is refused before any upload, with the
number that tripped.

Cost drift: when the measured cost misses the router's forecast beyond the noise floor, the
answer carries `meta.cost.drift = {predicted, measured, pct}` and the report gains a
`COST DRIFT` line. That line doubles as an instruction — relay it to the owner as-is; it
means the tariff table needs re-measuring. It is not an error, and the answer is unaffected.
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
