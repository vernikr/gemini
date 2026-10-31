# @vernikr/gemini

The npm package provides the `gemini` command and launches the Python gateway from PyPI
through `uv`. It installs `uv` with the official installer if needed; no system Python
installation is required.

## Install and run

```bash
pnpm dlx @vernikr/gemini setup
# or
npx -y @vernikr/gemini setup
```

Choose Direct mode for local keys and a local quota ledger, or Router mode to connect to a
fleet. The wizard can write an MCP client configuration and prints a verification step.

For AI-agent and non-interactive setup:

```bash
npx -y @vernikr/gemini setup --backend direct \
  --keys-file /absolute/path/keys.txt --write-client freebuff --json
```

## Automatic updates

On normal CLI starts, the launcher checks PyPI for a newer `vernikr-gemini` core and uses
it when available. The request has a short timeout; if the registry is unavailable or the
new release cannot be installed, the launcher uses the core version paired with this npm
package. `--help` and `--version` do not query PyPI.

Disable the update check for one command with `--no-update`, or set an environment variable
in the MCP client or shell:

```bash
GEMINI_AUTO_UPDATE=0 npx -y @vernikr/gemini setup
```

A globally installed npm launcher updates the Python core, not the launcher package itself.
To refresh the launcher, install or invoke the latest npm package (`npx @vernikr/gemini@latest`).

## Local text-to-speech

```bash
gemini tts notes.md --voice Kore --style "Clear, calm narration"
```

The command accepts UTF-8 `.txt` and `.md`, and writes a mono MP3. `ffmpeg` must be
available for encoding. An interactive terminal shows a progress bar that advances on
completed audio chunks and MP3 encoding; `--json` keeps output machine-readable.

## Development

The npm package contains a small launcher only. Gateway behavior lives in the Python
`vernikr-gemini` distribution. Its package version is the launcher's offline fallback; the
release workflow verifies matching Python/npm versions and publishes both artifacts.

```bash
npm test
```

See the [consumer guide](../../docs/06-consumer.md),
[architecture](../../docs/03-architecture.md), and
[release workflow](../../docs/09-ci.md).
