# Freebuff Desktop and Gemini

The gateway supports local Direct mode and a remote Router/Fleet mode. Use the setup
command when possible; it writes a client entry and selects the backend. Restart Freebuff
after changing MCP configuration.

## Direct mode

```jsonc
{
  "mcpServers": {
    "gemini": {
      "command": "npx",
      "args": ["-y", "@vernikr/gemini", "stdio"],
      "env": { "GEMINI_AUTO_UPDATE": "0" }
    }
  }
}
```

Configure API keys through `gemini setup --backend direct` or the private Gemini
configuration directory. Avoid embedding keys in `mcp.json`. The npm launcher checks PyPI
for a newer Python core by default; set `GEMINI_AUTO_UPDATE=0` to disable that check for
this client. It falls back to the launcher's pinned version when the registry is unavailable.

## Router mode over SSH

For a machine with SSH access to a deployed Gemini Router fleet:

```jsonc
{
  "mcpServers": {
    "gemini": {
      "command": "ssh",
      "args": ["<SSH_USER>@<YOUR_VPS_HOST>", "~/.local/bin/gemini", "stdio"]
    }
  }
}
```

The server process reads its provider keys and router configuration from its protected
service environment. SSH provides the transport; no bearer token is placed in the client
configuration. The setup wizard can provision and health-check the server when the operator
chooses Fleet mode.

## HTTP through a local SSH tunnel

The optional streamable-HTTP daemon binds to loopback. Forward its port through SSH rather
than exposing it directly to the network:

```bash
ssh -N -L 8790:127.0.0.1:8790 <SSH_USER>@<YOUR_VPS_HOST>
```

Then configure the local endpoint and provide the bearer token through the MCP client's
secret/environment facility:

```jsonc
{
  "mcpServers": {
    "gemini-http": {
      "type": "http",
      "url": "http://127.0.0.1:8790/mcp",
      "headers": { "Authorization": "Bearer ${GEMINI_MCP_TOKEN}" }
    }
  }
}
```

Do not expose the HTTP listener publicly without an explicitly secured network path. Store
`GEMINI_MCP_TOKEN` in the service and client secret stores; never paste it into source files
or logs. Rotate it through the deployment's approved credential process.

## Tools and file handling

| Tool | Use |
|---|---|
| `analyze_large` | Analyze long text with chunking and consolidation. |
| `gemini_generate` | Make one quota-aware completion request. |
| `analyze_result` | Poll an asynchronous analysis job. |
| `file_upload_ticket` + `analyze_file` | Upload and analyze a file without placing its bytes in the prompt. |
| `analyze_files` | Analyze several uploaded files in one task. |
| `quota_status` | Inspect model/key budgets and current availability. |
| `router_logs` | Inspect recent calls and provider outcomes. |

The file flow is: request an upload ticket, upload to its supplied URL, then pass the
returned upload ID to an analysis tool. The gateway extracts supported text and routes it
through the chunking pipeline. Provider-supported media is measured and sent as a file;
unsupported types and files outside configured limits fail before generation.

## TTS

Run `gemini tts notes.md` locally to create one MP3. The terminal shows an animated overall
progress bar: synthesis progress advances by completed chunks, and encoding completes the
bar. The percentage is deterministic for the known chunk count; waits and retries update
the status without guessing from elapsed time. `--json` emits machine-readable output
without a terminal bar.

## Check the connection

1. Restart Freebuff and start a fresh session.
2. Ask the client to list Gemini tools and call `quota_status`.
3. Submit a small, non-sensitive test prompt to `gemini_generate`.
4. For file analysis, use a small sample through the ticket/upload/analyze flow.
