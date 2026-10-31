# Consumer guide

Gemini runs against Google AI Studio in **Direct** mode or through a configured
**Router/Fleet** backend. Both modes expose the same MCP interface. A server is optional.

## Install and configure

```bash
pnpm dlx @vernikr/gemini setup
# or
npx -y @vernikr/gemini setup
# Python-only installation
uvx --from vernikr-gemini gemini setup
```

Choose Direct to use a local key pool and ledger, or Router to connect to a fleet service.
The wizard stores user configuration under `~/.config/gemini/`; key files are protected
with mode `0600`. It can print or write supported MCP client entries.

For non-interactive setup:

```bash
npx -y @vernikr/gemini setup --backend direct \
  --keys-file /absolute/path/keys.txt --write-client freebuff --json
```

The npm launcher checks PyPI for a newer Python core on normal CLI starts. If PyPI is
unreachable, it runs the Python version paired with the installed launcher. Disable the
check for one invocation with `--no-update`, or set `GEMINI_AUTO_UPDATE=0` in the client
environment. `--help` and `--version` do not query the registry. The Python package itself
can be version-pinned through `uvx --from 'vernikr-gemini==X.Y.Z' gemini ...`.

## TTS

```bash
gemini tts notes.md
gemini tts notes.txt --voice Kore --style "Warm, measured delivery"
gemini tts notes.md --output ~/Audio/notes.mp3 --bitrate 128
```

TTS accepts UTF-8 `.txt` and `.md`, writes one mono MP3, and uses `ffmpeg` for encoding.
The default bitrate is 128 kbps. `--voice`, `--style`, `--output`, `--bitrate`, and
`--no-wait` control synthesis. Direct mode uses the local key pool. Router mode reads the
proxy token over SSH into memory and uses a temporary loopback-only tunnel; the token is
not persisted in the local profile.

In an interactive terminal the TTS command displays a spinner, progress bar, and
percentage. Progress is deterministic: completed synthesis chunks advance the bar, and
MP3 encoding completes it. Quota waits and provider retries update the status text without
inventing time-based progress. `--json` produces machine-readable output without the bar.

## MCP configuration

The setup wizard can write a supported client's configuration. A typical local Direct
entry is:

```json
{
  "mcpServers": {
    "gemini": {
      "command": "npx",
      "args": ["-y", "@vernikr/gemini", "stdio"],
      "env": { "GEMINI_AUTO_UPDATE": "0" }
    }
  }
}
```

The `GEMINI_AUTO_UPDATE` setting is optional; omit it to keep automatic core updates
enabled. API keys should remain in the user's Gemini configuration directory or a
protected key file, not in the MCP JSON.

For Router mode over SSH:

```json
{
  "mcpServers": {
    "gemini": {
      "command": "ssh",
      "args": ["<SSH_USER>@<YOUR_VPS_HOST>", "~/.local/bin/gemini", "stdio"]
    }
  }
}
```

The SSH account needs access to the deployed service configuration. The setup wizard can
write the host and user to a local profile and can provision a fleet when requested.

The optional HTTP transport binds to loopback by default. For remote use, forward it over
SSH and keep the bearer token in the client/service secret stores. Do not expose the
listener publicly without an explicitly secured network path.

## Gateway tools

| Tool | Purpose |
|---|---|
| `analyze_large` | Chunk and consolidate a long text request. |
| `gemini_generate` | Make a single quota-aware completion request. |
| `file_upload_ticket` | Reserve a bounded, short-lived upload. |
| `analyze_file` | Analyze an uploaded file without inlining bytes into a prompt. |
| `analyze_files` | Analyze several uploaded files in one task. |
| `analyze_result` | Poll a job submitted for asynchronous completion. |
| `quota_status` | Read current model/key budgets and availability. |
| `router_logs` | Read recent calls and provider outcomes. |

For files, request a ticket, upload to its `upload_url`, and pass the returned `upload_id`
to an analysis tool. The gateway extracts supported text and uses its chunking pipeline;
media supported by Gemini is measured and sent as a provider file. Unsupported formats
and configured size/complexity limits fail before generation.

## Key and state management

```bash
gemini keys
gemini keys add <key>
gemini keys remove <index-or-fingerprint>
gemini quota
gemini doctor
```

`gemini keys` displays fingerprints rather than raw values. The command writes through the
configured key store and, where applicable, can restart the associated service. Direct
mode's usage ledger is local to the machine. Router mode uses the fleet's shared SQLite
ledger.

## Verify and troubleshoot

1. Restart the MCP client and start a new session.
2. Ask it to list Gemini tools and call `quota_status`.
3. Test a short, non-sensitive prompt with `gemini_generate`.
4. For files, run the ticket/upload/analyze flow with a small sample.
5. For local TTS, run `gemini tts sample.txt`; confirm the progress bar reaches 100% and
   the MP3 appears at the selected output path.

Use `gemini doctor`, `gemini quota`, and `gemini logs` for local diagnostics. Configuration,
backend selection, file processing, and HTTP settings are described in
[`docs/08-dual-mode.md`](08-dual-mode.md), [`docs/02-requirements.md`](02-requirements.md),
and [`docs/03-architecture.md`](03-architecture.md).
