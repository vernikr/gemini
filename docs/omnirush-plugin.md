# OmniRush plugin

The repository root contains the Claude Code-compatible plugin manifest and MCP server
configuration. The manifest starts the `gemini` gateway over stdio using the installed
checkout; MCP protocol messages use stdout, and diagnostics use stderr.

## Requirements

- OmniRush with support for `${CLAUDE_PLUGIN_ROOT}` in MCP commands.
- Python 3.12+ and `uvx` available to the OmniRush process.
- Gemini provider credentials configured in the user's runtime environment or
  `~/.config/gemini/`; never store keys in the repository or plugin manifest.

## Install

1. In OmniRush, open **Settings → Library → Advanced settings → From GitHub**.
2. Enter `https://github.com/vernikr/gemini`.
3. Preview the entry and confirm the plugin and MCP server are named `gemini`.
4. Install and enable the server, then start a new session.

The root `.claude-plugin/plugin.json` references `.mcp.json`. The MCP command uses the
installed plugin path, not a maintainer's workstation path. The source repository is
public; do not add authentication tokens to the manifest.

## macOS launch environment

Desktop apps may inherit a different `PATH` from an interactive shell. If OmniRush cannot
find `uvx`, make it available to the app or launch OmniRush from a configured terminal.
Avoid embedding user-specific absolute paths in the manifest.

## Verify

Run `python3 scripts/check-plugin.py` to validate the package files. After installation,
start a new session and ask the client to list Gemini tools and call `quota_status`. A live
provider request also requires configured Gemini credentials. The repository's automated
tests validate the manifest and server code; they do not exercise the OmniRush desktop UI.

Plugin metadata has its own version in `.claude-plugin/plugin.json`. `plugin-v*` tags are
reserved for plugin packaging and do not trigger the Python/npm package release workflow.
