# Architecture

## Components

`vernikr-gemini` is the gateway package and `gemini-router` is its quota-aware provider
library. The gateway exposes Gemini capabilities to MCP clients, while the router owns
provider requests, model selection, quota accounting, shared audit data, and its HTTP proxy.

- **CLI:** `gemini` configures the gateway, inspects quota and logs, manages skills, and
  creates local TTS audio.
- **MCP:** stdio is used by desktop/agent clients. The optional streamable-HTTP server is
  bearer-authenticated and binds to loopback by default.
- **Backend selection:** `direct` uses local API keys and a local SQLite ledger;
  `router` forwards work through a configured Gemini Router service and its shared ledger.
  `auto` selects from the available configuration. `GEMINI_MCP_BACKEND` can override it.
- **Provider router:** `gemini-router` selects an eligible key/model pair, applies the
  ledger-backed RPD/RPM/input-TPM gates, handles provider feedback and retries, and writes
  usage and trace data to SQLite.

## Request flows

### Text analysis

The gateway validates the request, measures or estimates input tokens, divides oversized
text into overlapping chunks, and sends each chunk through the router. Chunk results are
consolidated when needed. Progress stages and router events are reported to MCP clients and
recorded in application logs.

### Files

The caller requests a bounded upload ticket, uploads bytes, then submits the resulting
upload ID with an analysis task. The gateway extracts supported text and uses the same
chunking pipeline. Files that the provider handles directly are uploaded and measured by
`gemini-router`; they are not silently treated as text. The ticket, upload limit, and
retention settings are controlled by configuration.

### Text-to-speech

`gemini tts` accepts UTF-8 `.txt` and `.md` files. It chunks the text, routes each audio
request through `gemini-router`, assembles the returned WAV audio, and encodes one mono MP3
with the local `ffmpeg` executable. In direct mode it uses local keys. In router mode it
reads the proxy token over SSH into memory and opens a temporary loopback-only tunnel; the
token is not written to the local profile. File-level progress advances when a chunk
completes and when encoding completes, so the percentage represents completed work.

## State and credentials

- `gemini-router` stores quota, provider feedback, cache, call, job, trace, and note records
  in a SQLite database. The direct backend keeps that database on the local machine; a
  fleet deployment shares it through the router service.
- Gateway logs and configuration live under the user's Gemini configuration directory
  unless explicitly overridden.
- API keys and service tokens are runtime configuration. The code stores key fingerprints
  for audit display, not raw key values in logs.
- The HTTP endpoints bind to loopback unless an operator deliberately configures a different
  address. Public exposure requires a protected network path and bearer authentication.

## Source layout

- `src/gemini_mcp/`: CLI, configuration, backend selection, MCP tools/server, file ingest,
  application logging, skill installation, and the local TTS command.
- `src/gemini_router/` in the separate `gemini-router` repository: provider client, key and
  model selection, quota ledger, HTTP proxy, event model, and TTS assembly.
- `tests/`: unit and integration tests using mocked provider responses.
- `scripts/`: setup, deployment, secret scanning, and operational helpers.

## Release and deployment

The gateway's Python distribution and npm launcher are versioned together. `gemini-router`
ships a Python library/CLI and a separate npm HTTP-client helper. Tagged release workflows
run tests and publish artifacts to their package registries. The fleet deployment is a
separate operational step; package installation does not require a fleet server.
