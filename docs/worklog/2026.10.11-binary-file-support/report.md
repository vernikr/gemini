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
- Timings: the ~30 s first answer we first blamed on provider-side "ingestion" is **not**
  file handling — see the correction in §0.18.2, which also carries the measured cost model.

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

## 0.18.2: the owner challenged the numbers — re-measured, and two claims corrected

The owner asked whether the ~30 s first answer was real file-digestion time and whether the
token figures followed any official formula (images should scale with resolution, not KB).
Both points were right to challenge. Measured with `countTokens` (free) and `generateContent`
on the production key:

**Latency — there is no tens-of-seconds ingestion step.** A fresh file answers as soon as it
reports ACTIVE, and the wait is the model's, not the file's:

| question about a file uploaded seconds earlier | thinking | time | thinking tokens |
|---|---|---|---|
| PNG 384×384, one-word question | default | 10.1 s | 1450 |
| same, asked again | default | 8.0 s | 1214 |
| same, different question | default | 2.9 s | 341 |
| same, one-word question | low | 1.1 s | 0 |
| PDF 765 B (marker), first question | default | 1.5 s | 85 |
| PDF 765 B, first question | low | 1.9 s | 0 |
| PNG 384×384, first question | low | 4.8 s | 243 |

Upload → ACTIVE takes ~2 s even for 7 MB, at any size tested. The original 35.3 s was not
reproduced (the same PDF question answered in 1.5 s); one 503 "currently experiencing high
demand" was caught mid-experiment and the router's retry handled it. The dominant controllable
factor is the thinking budget: the default level spends 1200–1450 thinking tokens on a
one-word question. The `analyze_file` description and `docs/06` now say this — the earlier
"provider ingests the file" claim is retracted (the 0.18.1 edit to the description had
silently missed its anchor; both files carry the corrected text now).

**Token cost — set by `media_resolution`, not by bytes, and not by the 2.x tile rule.** The
count sits at 1089 across 300×300…4096×4096 px and 2 KB…7 MB (1600×1200 measures 1064 — within
2 %); two 1600×1200 JPEGs at 31 KB and 144 KB cost the same. The official per-image levels (Gemini 3 documents `media_resolution` low/medium/high
per media part):

| media | default | low | medium | high |
|---|---|---|---|---|
| image (all sizes tested) | 1089 (= high) | 256 | 529 | 1089 |
| PDF page (1 page, text) | 560 (= medium) | 280 | 560 | 1120 |
| PDF, 3 pages | 1680 (= 3 × 560) | 840 | — | — |

The published "≤384 px → 258, else 768×768 tiles" rule describes Gemini 2.x; on Gemini 3 the
per-image cost is flat per resolution level, and our requests currently take the default —
so images ride the most expensive level (1089) while a `media_resolution=low` part would cost
256. Also measured: the 1089-token figure holds for a 2 KB image and a 7 MB image alike, so
byte size never predicts cost; `countTokens` and `usageMetadata` agree (1098 in vs 1089
counted + prompt text).

**Probe budget (operational, not a product fact):** the serving key is on the provider's free
tier for `gemini-3.6-flash` — `GenerateRequestsPerDayPerProjectPerModel-FreeTier = 20` per
day. These experiments consumed the day's remaining generative requests; the counting tables
above cost nothing because `countTokens` draws no quota. Anything that spends should be
planned against that number until more keys (each project has its own allowance) or a paid
tier is in place.

## Cost notes kept for C2

- Per-image cost is flat per resolution level and byte-independent; the level is the lever:
  a `media_resolution=low` image part costs 256 against the default 1089, and a PDF page 280
  against 560. C2 decides the level per media type and sends it explicitly, instead of
  inheriting the provider default.
- Counting is free, so C2 plans packs by measurement — `count_file_tokens` is the input, and
  a per-page constant is never assumed (the 560 figure above is measured for this model).
