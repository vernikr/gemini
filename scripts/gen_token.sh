#!/usr/bin/env bash
# gen_token.sh — print a bearer token for the gemini-mcp HTTP daemon.
# Usage: bash scripts/gen_token.sh   → paste into .env GEMINI_MCP_TOKEN= and Freebuff env.
set -euo pipefail
openssl rand -hex 32
