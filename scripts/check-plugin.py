"""Offline checks for the Claude Code / OmniRush plugin package."""
import json
import pathlib
import re

root = pathlib.Path(__file__).resolve().parents[1]
plugin = json.loads((root / ".claude-plugin/plugin.json").read_text())
assert re.fullmatch(r"[a-z][a-z0-9-]*", plugin["name"])
assert re.fullmatch(r"\d+\.\d+\.\d+", plugin["version"])
assert plugin["mcpServers"] == "./.mcp.json"
config = json.loads((root / ".mcp.json").read_text())
assert len(config["mcpServers"]) == 1
for server in config["mcpServers"].values():
    assert server["command"] in {"npx", "uvx"}
    assert isinstance(server["args"], list) and all(isinstance(a, str) for a in server["args"])
    assert not server.get("env"), "Do not freeze user configuration or credentials"
    if server["command"] == "uvx":
        assert server["args"] == ["--from", "${CLAUDE_PLUGIN_ROOT}", "gemini", "stdio"]
        assert (root / "pyproject.toml").is_file()
    else:
        assert server["args"][0] == "--yes"
        assert re.fullmatch(r"@vernikr/[a-z-]+@\d+\.\d+\.\d+", server["args"][1])
print("Plugin manifest and MCP configuration: OK")
