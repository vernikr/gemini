# Item C implementation plan: option A — everything through the provider Files API

Owner's decision (2026-10-11): **A**. Rationale recorded: 48 h retention is fine, no
download is fine (the file is already with the agent), and 20 GB per project is out of reach
for this workflow. Two requirements came with it:

1. **Cheap token accounting for binary files** so a *group* of files can be split into
   feasible requests ("разбивать группы файлов на посильные чанки"). A single PDF is never
   split into parts.
2. Mixed PDFs in practice (text, scans, figure-heavy).

## Why A still costs more work than B

Not the upload protocol — start, upload+finalize, poll for `ACTIVE` is two calls and a
retry, and the router client already has the error taxonomy for it. The cost is that a
request built around file bytes stops being a plain string, so:

- the plan pre-flight has to reason about a request it cannot tokenize with the current
  estimator (solved below by measuring, not estimating);
- the provider gains a **second lifecycle owned by us** (uploaded files, their state and
  expiry) that the ledger has to mention without polluting per-model RPD accounting;
- a **new surface to mock** in the no-network test suite (upload start, finalize, state
  poll, expiry) plus one live probe to confirm limits against the real API.

B changes none of that: local conversion keeps every request a string. Given the owner's
clarifications (48 h is a feature, storage is unreachable), A's extra cost is worth paying
for model-quality answers on scans and figures.

## Owner corrections (2026-10-11), adopted

**1. The boundary is extractable vs not, not text vs binary.** Text the server can already
pull out of a file — pandoc set, our RTF parser, and PDFs with a text layer via the new
poppler extraction — keeps the existing pipeline: chunking, overlap, consolidation, the
content cache. Chunking does not stop applying; it stops applying only to the bytes we
hand to the provider (scans, figure-heavy pages, images, audio). So the routing rule is:

```
file → can we get text? → yes → existing text pipeline (chunkable, cacheable, cheap)
                          → no  → Files API: one request per file, measured tokens
```

**2. Cache decision: key on content, never on the provider URI.** A cache key that
includes `file_uri` would miss every time the file is re-uploaded — after 48 h, or on a
different key — and re-spend on work already done. A key that ignores the file would let
two different files collide under one task. So the key extends today's content hash with
`sha256(file bytes) + mime` for the binary path, and with the extracted text for the
extractable path. The URI stays out of the key, which makes a cache hit survive re-uploads
and travel across keys. Our cache stores results, not files, so provider expiry never
invalidates it.

**3. Every upload is per key.** The owner's keys are separate Google accounts, so a file
uploaded with one key is invisible to the next. That fixes the request order: the router
picks the serving key first (chain × budget, as today), the upload happens with that key,
and a retry on another key re-uploads — a cheap operation next to the analysis it enables,
and only on failure. The URI cache is therefore keyed by `(key_id, file hash)`; on failure
the retry path re-uploads once for the new key and caches that too. If the measured tokens
cannot fit any key's remaining per-minute budget, that is a pre-flight refusal carrying the
measured number and the reset ETA, exactly like the existing quota refusal — never a
half-sent request.

## Phase C1 — one file, one request

**Routing inside `analyze_file`:** extract where possible (pandoc set, RTF, PDF text layer),
otherwise go to the Files API. A PDF that yields no meaningful text — a scan — stops being
an error and becomes the case the Files API exists for. Page count and size ceilings are
checked here, before any upload.

**Router (`gemini-router`), the quota authority and the API client:**

- `upload_file(key, path, mime)` → resumable upload: `POST {base}/upload/v1beta/files` with
  `X-Goog-Upload-Protocol: resumable` + `Command: start` → `x-goog-upload-url`; then the
  bytes with `Command: upload, finalize` → `{file: {name, uri, state, expirationTime}}`.
  The blob is streamed (`content=<file object>`), never base64 — RAM must not grow.
- `file_state(key, name)` → poll until `ACTIVE` (small files are instant; do not assume).
- `delete_file(key, name)` → optional tidy-up when our inbox blob is pruned.
- `count_file_tokens(key, model, uri, mime)` → `countTokens` with a file-data part: the
  exact number, one call, and by FR-C7 it does not draw on RPD.
- `generate()` gains optional extra parts (file references) beside the text prompt.
- Ledger: an `upload` event row per upload (bytes, key_id, provider file name) that does
  **not** touch RPD/RPM model counters. `router_logs` can show it; the 20 GB budget stays
  observable by listing files.

**Gateway (`gemini-mcp`), the flow:**

- `analyze_file` stops failing on the supported binary set (PDF, PNG/JPEG/WebP/HEIC,
  WAV/MP3/M4A/OGG/FLAC, MP4) and instead: upload → measure tokens → one analysis request
  with the file reference + the task.
- Progress gains stages `uploaded` (bytes, measured tokens) and `analyzing`; `meta.file`
  gains the provider name/uri/expiry/tokens.
- The upload sidecar (`inbox/<id>.json`) caches `{provider_name, uri, expires}`; a retry
  re-uploads only when the cached reference is stale or missing. Our own `keep_hours` (24 h)
  is shorter than the provider's 48 h, so no new failure mode appears.
- Pre-flight, zero spend, mirroring `file_too_large`: unsupported MIME, and the document
  ceilings the provider enforces at analysis time regardless of the 2 GB staging limit
  (PDF size and page count). Numbers to confirm by live probe, never hardcoded.
- MIME and duration from the blob itself: extend the magic sniff for audio/video, and use
  `ffprobe` (already on the VPS via ffmpeg) for duration — one subprocess, no Python
  dependency. Page count needs a PDF tool; `poppler-utils` is the licence-clean option
  (a subprocess, like pandoc), installed by `setup_server.sh`.
- Inline base64 stays out entirely: it inflates by a third and lands in a buffered body.

## Phase C2 — groups of files, packed by measured tokens

The existing pipeline already knows how to split a job into requests and consolidate the
answers; files are simply inputs whose size we measure instead of estimate. So:

- a file-groups call accepts several upload ids; **extractable** files join the text
  pipeline first (their text is chunkable like any other body), while **binary** files are
  uploaded and token-measured, then packed into requests that fit the chosen key's
  remaining per-minute budget and context window;
- per-group progress and per-group ledger rows, then the existing consolidation stage
  produces the single final answer;
- one PDF is never split — a file that alone exceeds the budget is refused with its
  measured cost, not truncated.

## Verification

- respx mocks for the upload lifecycle and the file-data request shape; no network in CI.
- One live probe on the server (a small PDF, an image and a short audio clip through the
  real API) to confirm the limits, the token rates and the `ACTIVE` timing, recorded in the
  worklog report. Provider numbers go into the docs as "resolve/measure", not constants.
