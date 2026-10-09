#!/usr/bin/env bash
# rotate_token.sh — mint a fresh GEMINI_MCP_TOKEN on the server, restart the daemon,
# print the new token ONCE (pipe it, never hand-copy — postmortem p01).
# Mac side afterwards:
#   ssh root@<vps> 'grep GEMINI_MCP_TOKEN /opt/apps/gemini-mcp/.env | cut -d= -f2 | tr -d "\n"' | pbcopy
#   …or re-run the python mcp.json rewrite snippet from docs/freebuff-wiring.md
set -euo pipefail
ENVF=/opt/apps/gemini-mcp/.env
NEW=$(openssl rand -hex 32)
grep -v '^GEMINI_MCP_TOKEN=' "$ENVF" > "$ENVF.tmp" || true
echo "GEMINI_MCP_TOKEN=$NEW" >> "$ENVF.tmp"
mv "$ENVF.tmp" "$ENVF"; chmod 600 "$ENVF"
systemctl restart gemini-mcp.service
sleep 2
systemctl is-active gemini-mcp.service
# AX/hygiene (postmortem p01): never print the full token by default — pastes and
# terminal uploads leak it. Fetch over ssh pipe instead; --print only when alone.
if [[ "${1:-}" == "--print" ]]; then
  echo "$NEW"
else
  echo "NEW TOKEN stored. masked: ${NEW:0:8}…${NEW: -4}"
  echo "fetch: ssh root@<vps> 'grep GEMINI_MCP_TOKEN /opt/apps/gemini-mcp/.env | cut -d= -f2 | tr -d \"\\n\"' | pbcopy"
fi
