# @vernikr/gemini-mcp

One-command Gemini **free-tier** MCP gateway (Python core: `gemini-mcp-gateway` on PyPI,
bootstrapped via [uv](https://docs.astral.sh/uv/) — no system Python needed). Works
**without any server**: default `direct` mode talks to Google AI Studio straight from
your machine; `router` mode joins a fleet gateway with a shared quota ledger.

```bash
pnpm dlx @vernikr/gemini-mcp setup     # wizard: AI Studio keys → ~/.config/gemini-mcp
                                       #         → live key probe → client snippets
pnpm dlx @vernikr/gemini-mcp doctor    # self-check with fix commands
pnpm dlx @vernikr/gemini-mcp quota     # free-tier budget table
pnpm dlx @vernikr/gemini-mcp uninstall --yes   # remove keys+config
```

Features: quota-aware routing across the Flash chain (two routes: `optimal` /
`best_available`), massive-body chunked analysis with consolidation, file ingest with
server-side RTF/DOCX→Markdown extraction, async tickets for long jobs, structured
agent-facing errors. Repo: github.com/vernikr/gemini-mcp
