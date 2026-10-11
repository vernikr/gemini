# Item C implementation plan: binary files through the provider Files API

Owner's decisions (2026-10-11): **option A** — the provider reads the bytes; and **PDFs go
to Gemini as they are, without local text extraction**, because local converters mangle PDF
text and a page mixes text with images anyway. Do not overcomplicate the backend: Google's
reader is better.

The router's half — the capability and its interface — lives in
`vernikr/gemini-router → docs/worklog/2026.10.11-binary-file-support/plan.md` and is stated
there once. This file is the product half: what the gateway does with it.

## Routing rule inside the file flow

```
file → PDF, image, audio, video        → Files API: one file, one request, measured tokens
     → pandoc set / RTF / plain text   → existing text pipeline: chunking, cache, groups
```

Text documents keep every property they have today — chunking with overlap, consolidation,
the content cache. That is what "text can still be split" means in practice; the rule that
changed is PDFs, which no longer go anywhere near a local extractor. Poppler appears only
as `pdfinfo`, for the page count the pre-flight needs, never for content.

## Phase C1 — one file, one request (gateway half)

- `analyze_file` accepts the binary set and, for it: choose the serving key → upload with
  it → measure tokens (`count_file_tokens`) → one analysis request with the file reference
  pinned to that key. Progress gains `uploaded` (bytes, measured tokens) and `analyzing`;
  `meta.file` gains the provider name, expiry and measured tokens.
- **Cache keys on content**: the request hash mixes in the file's content hash, never the
  provider URI. A hit therefore survives re-uploads and travels across keys, and provider
  expiry cannot invalidate it. (Same rule the router plan honours.)
- **A failed key re-uploads.** Files belong to the key's project, so a retry on another key
  uploads once more from our blob before sending; the URI cache is keyed by
  `(key_id, content hash)`.
- **Pre-flight, zero spend, before any upload**: unsupported MIME, size, and the document
  ceilings the provider enforces at analysis time regardless of the 2 GB staging limit.
  Numbers are confirmed by live probe and recorded as measured facts, never hardcoded.
  A file that cannot fit any key's remaining per-minute budget is refused with its measured
  cost and the reset ETA — the same shape as the existing quota refusal.
- MIME from the blob's own magic bytes (extending the current sniffer), duration via
  `ffprobe` (already present with ffmpeg), page count via `pdfinfo` (`poppler-utils`,
  installed by `setup_server.sh` — a subprocess, like pandoc, so no Python dependency and
  no licence change).
- The upload sidecar (`inbox/<id>.json`) caches `{key_id, provider_name, expires}`.

## Phase C2 — groups of files, packed by measured tokens

- A file-groups call accepts several upload ids. Each **binary** file is uploaded and
  token-measured; packs are then built against the chosen key's remaining per-minute
  budget and context window. **Text** files join the existing pipeline first, where their
  text is chunkable like any other body.
- Per-group progress and per-group ledger rows, then the existing consolidation stage
  produces the single final answer.
- One PDF is never split. A file that alone exceeds the budget is refused with its measured
  cost, not truncated.

## Verification

- respx mocks for the whole flow; no network in CI. Each new rule ships with a test that
  fails on the old code.
- One live probe on the server (a text PDF, a scan, an image, a short audio clip) confirms
  the ceilings, the token rates, the `ACTIVE` timing and the per-key visibility; findings
  recorded in the worklog report and referenced from the docs as measured, not assumed.
- Documentation kept true: `docs/02-requirements.md` gains the file-input requirement,
  `docs/03-architecture.md` §10 states the routing rule, `docs/06-consumer.md` and
  `docs/freebuff-wiring.md` describe the flow the agent sees.
