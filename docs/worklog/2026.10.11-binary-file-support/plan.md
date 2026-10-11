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
retry, and the router client already has the error taxonomy for it. The cost is that A
changes the shape of a model request in a codebase built on "a prompt is a string":

- the request carries a **file reference part**, so the pipeline's chunking cannot apply —
  bytes are one request and one answer, and the plan pre-flight has to reason about
  something it cannot tokenize with the current estimator;
- the provider gains a **second lifecycle owned by us** (uploaded files, their state and
  expiry) that the ledger has to mention without polluting per-model RPD accounting;
- a **new surface to mock** in the no-network test suite (upload start, finalize, state
  poll, expiry) plus one live probe to confirm limits against the real API.

B changes none of that: local conversion keeps every request a string, so chunking,
caching, progress and the ledger keep working untouched. That is the whole difference —
and given the owner's clarifications (48 h is a feature, storage is unreachable), A's
extra cost is worth paying for model-quality answers on scans and figures.

## Phase C1 — one file, one request

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
answers; files are simply chunks whose size we measure instead of estimate. So:

- `analyze_file` (or a sibling tool) accepts several upload ids; each is uploaded and
  token-measured, then packed into requests that fit the remaining TPM/context budget;
- per-group progress and per-group ledger rows, then the existing consolidation stage
  produces the single final answer;
- one PDF is never split — a file that alone exceeds the budget is refused with its
  measured cost, not truncated.

## Verification

- respx mocks for the upload lifecycle and the file-data request shape; no network in CI.
- One live probe on the server (a small PDF, an image and a short audio clip through the
  real API) to confirm the limits, the token rates and the `ACTIVE` timing, recorded in the
  worklog report. Provider numbers go into the docs as "resolve/measure", not constants.
