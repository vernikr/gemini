# Item C1 closed: PDFs, images and audio reach the model through the provider Files API

Released as **gemini-mcp 0.18.0**, hotfixed as **0.18.1**. The library half — the capability
and its interface — is gemini-router 0.1.12, recorded in
`vernikr/gemini-router → docs/worklog/2026.10.11-binary-file-support/report.md`.

C2 (groups of files packed by measured tokens) is **not** in this report; it stays open in the
plan in this folder.

## What changed

- `analyze_file` routes by the blob's own bytes, magic first: PDF, PNG, JPEG, GIF, `OggS`,
  `fLaC`, `ID3`, RIFF WAVE/WEBP, MP3 frame sync, `ftyp` M4A (audio) vs isom (video). The
  extension is a hint only when the bytes are non-text — so a text file named `.mp4` stays
  text and keeps the pipeline it always had.
- Binary path: pre-flight (MIME, size, page count via `pdfinfo` — zero spend, before any
  upload) → upload pinned to the chosen key → `count_file_tokens` → ONE request carrying the
  file reference, with `est_tokens = measured + len(task) // 4`. The file is never split.
- `meta.file` reports `{filename, bytes, mime, tokens, via, provider{name, uri, expires_at,
  key_id}}`; progress gains `uploading` / `uploaded` (with the measured count).
- **Reuse:** the provider reference lives in the inbox sidecar and is reused while
  `expiry − now > 600 s` and the key is still configured; otherwise the blob is uploaded
  again. A failed key re-uploads under the next one — the keys are separate accounts, so a
  file uploaded under one is invisible to another.
- **Text documents are untouched**: the pandoc set, RTF and plain text keep the chunked
  pipeline with consolidation and the content cache. PDFs are the only thing that moved —
  they now go to the provider exactly as they are, because local converters mangle their text
  and a page mixes text with images.
- Errors: `pdf_too_large` / `media_too_large` / `pdf_too_many_pages` → 413,
  `files_api_disabled` → 415, `file_upload_failed` → 502.
- `setup_server.sh` installs `poppler-utils`; Poppler appears only as `pdfinfo`, never for
  content (the AGPL PyMuPDF route was rejected on licence grounds).
- Docs: `FR-B10` in 02, the §10 routing rule in 03, plus 06 / freebuff-wiring / README.

## Live probe on the production daemon

Deployed 0.18.0, `gemini-3.6-flash`, serving key `b41654f0`, one real PDF and one real PNG:

| file | bytes | measured tokens | provider in/out | first answer | second call |
|---|---|---|---|---|---|
| probe.pdf (marker `AZURE-COMPASS-77`) | 765 | **560** | 541 / 31 | 35.3 s | 3.3 s, same `files/8th0shqvpjiw`, **no re-upload** |
| probe.png (32×32 solid red) | 99 | **1089** | 1101 / 5 | 30.0 s | — |

- The PDF's marker came back correctly; the PNG was described as "a solid red square".
- Image tokens are **per image and effectively independent of byte size** (99 B → 1089
  tokens) — the fact that makes group packing in C2 token-cheap.
- Provider expiry is +48 h (`2026-10-13T03:28:13Z`), matching the documented Files API
  retention; the sidecar stores it, so reuse needs no re-upload and no guesswork.
- The first request after an upload takes ~30 s while the provider ingests the file even
  though it already reports ACTIVE; the second answers in seconds. Both the `analyze_file`
  description and `docs/06` now say so, so a slow first answer is not read as a hang and the
  agent does not resubmit.

## 0.18.1: the probe found a reporting bug

Every live report said `rough_edges=1`, including the clean runs. Cause: the router's
`routing` event message embeds its detail dict, and that dict contains a `wait` key — so the
plain keyword scan matched it on every single request. The signal whose whole job is to tell
the agent "something needed retrying or waiting" was therefore always on, i.e. worth nothing.

Rough edges are now decided by **stage** (`router:retry | waiting | switch | pacing |
overflow | quota_exhausted`), with the keyword scan kept for non-router messages and `wait`
dropped from it. A new test pins both directions — the routing detail dict is not an edge, a
`router:waiting` stage is — and the old code fails it (mutation-checked). Re-verified live on
0.18.1: the report reads `… · 0.0s · clean run`, with the provider file reused.

## Evidence

- Tests 121 → 139 (0.18.0) → 140 (0.18.1). Four gateway mutations, each caught by
  `tests/test_files_api.py`: `est_tokens` without file tokens; `up=None` disabling reuse;
  `pages=None` skipping the page ceiling; PDF magic removed from the sniffer.
- The fake router in `conftest.py` gained the Files API (`upload_file`,
  `count_file_tokens`, `complete(files=…)`, expiry/error knobs) — and it is exactly why the
  rough-edge bug survived CI: the real router emits a `routing` event the fake never did.
- `test_binary_fail_fast_zero_spend` was re-pinned on gzip bytes: PDFs are what this change
  made readable, so the old expectation was the thing that had to move.
- Release runs green on both tags: v0.18.0 — test 18 s / pypi 23 s / npm 9 s / deploy 32 s /
  smoke 146 s; v0.18.1 — same jobs, 205 s wall clock. PyPI and npm serve 0.18.1 (0.18.0
  superseded), the VPS daemon reports 0.18.1 with `gemini-mcp.service` active.
- The tag builds run `release` only; the push to `main` runs `ci` — the earlier dedup holds.

## Cost notes kept for C2

- Images cost a flat per-image amount regardless of pixels; audio is priced by duration.
  Google's token economics for images are favourable enough that packing several small images
  into one group request is cheap.
- Counting is free (no RPD), so C2 plans packs by measurement, not by estimate. The published
  per-page PDF guidance did not survive contact with reality — a one-page text PDF measured
  **560** tokens — which is exactly why the packer takes `count_file_tokens` as its input and
  never a per-page constant.
