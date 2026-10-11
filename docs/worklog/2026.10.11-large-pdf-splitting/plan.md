# F · Oversized PDF splitting · owner decision and implementation, 2026-10-11

**Owner decision (2026-10-11):** splitting IS allowed — but only when a single PDF does
not fit the key's FULL minute token quota. Owner constraint: when one file of a
multi-file request is split, its parts must never be mixed with other files' parts in
one request/consolidation — the model must not be confused.

## Measured facts (VPS, 950-page fixture)

qpdf 11.9 page-range split: ~0.2–0.3 s per chunk, observed peak RSS ~13 MB
for a 950-page/1.6 MB synthetic fixture. This is a measurement on that fixture,
not a guaranteed upper bound; provider upload/generation dominate end-to-end time.
PDFs are already capped at ingest (pdf_max_mb=50, pdf_max_pages=1000).

## Flow (analyze_file single, analyze_files group)

1. Measure the uploaded PDF for free at its resolution (default high).
2. Measured + prompt ≤ full minute quota (limits_per_key_model.tpm) → today's
   single request; do not split due to a temporary minute gate. Keep the requested
   media resolution unchanged (default high); no automatic quality downshift.
3. Larger → **split**: estimate a conservative initial span from measured mean
   page cost and prompt reserve. Cut consecutive page ranges with qpdf (missing
   binary → loud ⛔ refusal); upload/re-measure each chunk at that SAME level.
   If the measured chunk exceeds the full quota, bisect it before generating;
   even one page too large → structured refusal. Each request carries only ONE
   file's PDF part, sequential requests — the router waits out minute gates
   (FR-C13, visible via FR-E10 live eta).
4. Chunk answers consolidate per-file first (existing recursive fan-in), the per-file
   answer then joins the group consolidation as ONE part — split parts never share a
   consolidation round with other files.
5. Guards: non-PDF binaries over budget still refuse (they cannot be split); ≤ 16
   requests per file, else refuse; scratch chunk files are removed after the job.
   Some errors include `completed_parts`; after partial success quota may already
   have been spent. The agent must never promise zero spend in that case.

Meta: `files[].split = {pages, chunks, ranges[], resolution}`; report carries the
⚠️ convention (splitting is a user-visible cost/degradation, not an error).

## Other owner request: visible severity and weak-model attribution

A response may use several models. Show the distinct serving models in the report,
mark EACH unrequested flash-lite/gemma model with ⚠️ and tell the agent to relay it;
explicitly pinned weak model is not an unexpected fallback. Other degraded outcomes
(waits, retries, partial chunks, cost drift, split PDFs) get ⚠️; a failed tool call
gets ⛔️ in the agent's reply, with a recovery hint. A clean run gets neither.
The MCP server instructions carry the policy; the report encodes successful warnings.

## Publication and live verification

Gateway v0.24.0: full battery 198 pytest tests + ruff + secrets guard green;
release jobs test/PyPI/npm/deploy/smoke green. Production daemon reports 0.24.0;
qpdf 11.9 installed. Live `analyze_files` probe with a real 230-page PDF and a
separate 1-page PDF: initial measured cost 257,600 tokens at `high` exceeded full
250,000 TPM → split into pages 1–200 and 201–230 (TWO chunk requests), then
consolidated that PDF to ONE answer before the other file was joined. The other
file was alone in its group (`groups: [real.pdf]`), final `parts: 2`. The answer
identified the repeated page text correctly. The `⚠️` report and live waiting
countdown were visible. End-to-end 216.5s (provider quota waits/cooldowns and
model calls dominated; this is NOT the cutting time). No source-file mixing.
