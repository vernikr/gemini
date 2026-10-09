# @vernikr/gemini-mcp

One-command Gemini **free-tier** MCP gateway (Python core: `gemini-mcp-gateway` on PyPI,
bootstrapped via [uv](https://docs.astral.sh/uv/) — no system Python needed).

```bash
pnpm dlx @vernikr/gemini-mcp setup     # wizard: keys → token → daemon → client snippets
pnpm dlx @vernikr/gemini-mcp doctor    # self-check with fix commands
pnpm dlx @vernikr/gemini-mcp quota     # live free-tier budget table
```

Features: quota-aware routing across the Flash chain (two routes: `optimal` /
`best_available`), massive-body chunked analysis with consolidation, file ingest with
server-side RTF/DOCX→Markdown extraction, async tickets for long jobs, structured
agent-facing errors. Repo: github.com/vernikr/gemini-mcp
