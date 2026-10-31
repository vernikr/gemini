# Gemini

A Gemini MCP gateway for long inputs, files, quota-aware model routing, and local
text-to-speech. It runs either directly against Google AI Studio or through a
`gemini-router` fleet; both modes expose the same gateway tools.

## Install and configure

```bash
pnpm dlx @vernikr/gemini setup
# or: npx -y @vernikr/gemini setup
```

Choose **Direct** to use a local API-key pool and ledger, or **Fleet** to connect to a
router service you operate. The setup command guides key entry, writes local configuration,
and can merge the MCP server entry into supported client configurations.

The npm launcher checks PyPI for a newer Python core at CLI startup. If the registry is
unavailable, it uses the version paired with the launcher. Disable the check for a command
with `--no-update`, or for an MCP client or shell with `GEMINI_AUTO_UPDATE=0`. `--help` and
`--version` do not make a registry request. Direct `uvx` installs can select a version
explicitly:

```bash
uvx --from 'vernikr-gemini==<VERSION>' gemini --version
```

For an AI agent or non-interactive setup, pass all choices explicitly:

```bash
npx -y @vernikr/gemini setup --backend direct \
  --keys-file /absolute/path/keys.txt --write-client freebuff --json

npx -y @vernikr/gemini setup --backend router --vps-host <YOUR_VPS_HOST> \
  --vps-user <SSH_USER> --projects-dir ~/projects \
  --write-client freebuff --json
```

The setup command prints a verification prompt after configuration. Restart the MCP client,
then ask its Gemini server to run the prompt's `quota_status` check. See the
[consumer guide](docs/06-consumer.md) for client configuration and deployment details.

## Capabilities

- **Large text inputs:** overlapping chunks are analyzed individually and consolidated;
  structured progress, logs, and request traces are available to the caller.
- **Files:** upload tickets accept supported documents, images, and audio. Extractable text
  uses the chunked pipeline; media supported by Gemini is sent as a measured provider file.
- **Routing:** the router backend shares a SQLite quota ledger across clients. Direct mode
  uses a local ledger and does not require a server.
- **TTS:** `gemini tts file.md` creates a mono MP3 from UTF-8 `.txt` or `.md` input. The CLI
  shows a live overall progress bar: completed chunks advance the percentage, and MP3
  encoding completes it. The progress is based on completed work, not a time estimate.
- **Operations:** `gemini quota`, `calls`, `logs`, `doctor`, and `prune` inspect local or
  shared state without making an unrequested generation call.

The TTS command accepts `--voice`, `--style`, `--output`, `--bitrate`, and `--no-wait`.
Direct mode uses local keys. Router mode reads the proxy token over SSH into memory and
forwards requests over a short-lived loopback-only tunnel. `ffmpeg` must be installed for
MP3 encoding.

## MCP connection

The setup command can write client configuration. For a manual Fleet connection over SSH:

```jsonc
{
  "mcpServers": {
    "gemini": {
      "command": "ssh",
      "args": ["<SSH_USER>@<YOUR_VPS_HOST>", "~/.local/bin/gemini", "stdio"],
      "env": { "GEMINI_AUTO_UPDATE": "0" }
    }
  }
}
```

For Direct mode, run the npm launcher as the MCP command and keep API keys in the gateway's
private configuration directory rather than embedding them in the client JSON. See
[`docs/06-consumer.md`](docs/06-consumer.md) and
[`docs/freebuff-wiring.md`](docs/freebuff-wiring.md).

## Development

This checkout requires Python 3.12 or later. To run the test suite and linter:

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check src tests
python scripts/check_secrets.py
npm test --prefix packaging/npm
```

Tests mock provider responses and do not require live API keys. For a router change, run
the tests and checks in the `gemini-router` repository as well.

## Documentation

- [Requirements](docs/02-requirements.md)
- [Architecture](docs/03-architecture.md)
- [Consumer setup and client configuration](docs/06-consumer.md)
- [Direct and router backends](docs/08-dual-mode.md)
- [CI, package publishing, and releases](docs/09-ci.md)
- [OmniRush plugin](docs/omnirush-plugin.md)
