# Direct and Router backends

The gateway exposes the same MCP tools and request semantics in both modes. The difference
is where provider credentials and quota state are held.

## Backend selection

Set `backend` in the gateway configuration or use `GEMINI_MCP_BACKEND`:

- `direct`: local API-key pool and local persistent SQLite ledger.
- `router`: configured Gemini Router endpoint and its shared ledger.
- `auto`: selects `router` when `GEMINI_ROUTER_CONFIG` is set or the configured router
  state file exists; otherwise selects `direct`.

The environment variable overrides the configuration value. Direct-mode state defaults to
`~/.config/gemini/state.db`; an explicitly configured state path takes precedence.

## Mode comparison

| Area | Direct | Router |
|---|---|---|
| Provider keys | Local Gemini key pool | Key pool managed by the router service |
| Quota ledger | Persistent on this machine | Shared by clients of the fleet |
| Cross-machine accounting | No | Yes |
| MCP tools and request behavior | Same gateway interface | Same gateway interface |
| Local `gemini tts` | Uses local keys and ledger | Uses a temporary SSH tunnel to the router proxy |

The router backend is appropriate when multiple clients or machines need one quota view.
Direct mode needs no server and keeps its state local.

## Text-to-speech

`gemini tts` runs on the caller's machine in either mode. Router mode reads the proxy token
over SSH into memory, binds a short-lived tunnel to loopback, and closes it on completion
or failure. The local profile stores only the configured SSH host and user. Direct mode
uses the local key pool. The CLI progress bar advances as chunks complete and as MP3
encoding completes; it does not estimate progress from elapsed time.

## Keys and state

Direct mode accepts keys through the supported environment variables, a protected key file,
or the gateway's private configuration directory. Key display uses fingerprints rather
than raw values. The router service reads its keys from its protected runtime environment.

Both modes use the `gemini-router` request engine. Direct-mode key probes use an in-memory
SQLite ledger; normal calls persist to the selected state database. The CLI exposes `quota`,
`calls`, `doctor`, and related commands for the active backend.

## Local update behavior

The npm launcher for `@vernikr/gemini` checks PyPI for a newer gateway core on normal CLI
starts. Use `--no-update` for one invocation or `GEMINI_AUTO_UPDATE=0` to disable it in a
client environment. Direct Python installations remain controlled by the version selected
through `uv`.
