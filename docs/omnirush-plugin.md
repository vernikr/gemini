# OmniRush / Claude Code plugin

## Prerequisites

Install uv (including uvx) and Python 3.12+. The plugin builds the Python package from this checkout, not a nonexistent npm/PyPI distribution. The pinned gemini-router dependency is loaded from its private GitHub tag because the required version is not yet on PyPI. Git and read access to vernikr/gemini-router are required in the app environment (configure the system Git credential helper first; do not embed credentials in the manifest). Configure Gemini first with `uvx --from /absolute/path/to/plugin gemini setup` (or provide the supported environment/configuration). API keys belong in the runtime environment or Gemini user config, never in this repository.

## Install from GitHub

1. Open OmniRush **Settings → Library → Advanced settings → From GitHub**.
2. Paste `https://github.com/vernikr/gemini`.
3. Select **Preview** and verify the plugin name `gemini` and MCP server `gemini`.
4. Select **Install**, enable the server if prompted, then start a new session.

For a private repository, GitHub authentication must already be configured on the computer running OmniRush. Never paste a PAT into a manifest or commit it.

The root `.claude-plugin/plugin.json` is the plugin entry point; `mcpServers` references the root `.mcp.json`. Gemini uses the standard `${CLAUDE_PLUGIN_ROOT}` substitution to run the installed checkout, not a path on the maintainer's computer. OmniRush must support this Claude Code placeholder. These are stdio servers; stdout is reserved for MCP.

## macOS launch environment

OmniRush launched from the Dock may not inherit shell PATH or environment settings. Ensure the prerequisite command (`uvx`) is discoverable in the app environment; if needed, launch OmniRush from a configured terminal or adjust its MCP environment settings. No personal absolute paths or secrets are embedded in the plugin.

## Verify and troubleshoot

Run `python3 scripts/check-plugin.py` to validate packaging. A successful preview is not a live MCP check: after installation, ask the client to list tools and call an appropriate health/status tool. Gemini needs configured provider credentials for real analysis; Browser Agent needs its external daemon for browser operations. Biblio mirror availability is independent of installation.

If Preview rejects the manifest or fails to resolve the placeholder, record the OmniRush version and error; do not replace it with a guessed local absolute path. The desktop UI cannot be exercised by this repository's offline tests.

## Versioning

Plugin packaging has its own SemVer in `.claude-plugin/plugin.json`. Tags use `plugin-v*`, so packaging releases do not trigger server-package publishing or infrastructure deployment workflows. Existing server release/version contracts remain unchanged.
