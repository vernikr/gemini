# Item C decision brief: PDFs, images and audio

## Where we stand

`analyze_file` extracts text on the server (pandoc for RTF/DOCX/ODT/EPUB/HTML, our own RTF
parser) and feeds the existing chunking pipeline. Anything without a text layer fails fast
with `binary_file_needs_conversion`, spending nothing. So the gateway handles anything it
can turn into text, and nothing else: no PDFs, no images, no audio.

## Three ways to close the gap

|  | **A · Provider Files API** | **B · Local converters** | **C · Split by modality** |
|---|---|---|---|
| What happens | Bytes go to Google once, the request references the returned URI | Everything becomes text first: PDF → text, image → OCR, audio → transcript, via the same one-shot subprocess pattern pandoc already uses | PDF → local text extraction; images and audio → Files API |
| Cost | ~2 extra provider calls + a state poll + URI caching; stage bounded by 20 GB/project, 48 h expiry; a new surface to mock in tests | apt packages on the VPS (poppler, tesseract + language data); CPU minutes on the single core next to the external network services; one new tool-specific failure mode each | Both bills, each applied where it clearly wins; one routing rule to keep honest |
| Quality | Best: the model reads real pages, figures, charts, handwriting; audio understood natively | Text-layer PDFs are exactly right; OCR of scans loses layout and figures; audio needs a local speech model this box cannot run in time | Text PDFs: exact text, cheap, chunkable, cacheable. Media: model-quality answers |
| Fits the existing pipeline | No chunking — bytes are one request, one answer; the massive-body machinery does not apply | Yes, unchanged: chunking, caching, ledger and progress all keep working | Yes for PDFs (the common case); media is single-shot, which is correct for it anyway |
| Main risk | The file cannot be downloaded back; storage and retention belong to the Google project, not to a key | A scanned or figure-heavy PDF silently degrades to "OCR mush" and nobody notices | Two paths to keep true — state the split in one place or it drifts |

Effort: A is M, B is M, C is L. All are multi-session chunks, none is a single sitting.

**One option not listed, on purpose.** An earlier note (`docs/freebuff-wiring.md`) names
`pymupdf4llm` as the PDF converter candidate. PyMuPDF and pymupdf4llm are AGPL-3.0, and
this project is MIT and distributed on PyPI — an AGPL dependency changes the licence of the
published package. Poppler's `pdftotext` is a separate program invoked the way pandoc
already is: no licence change, no new Python dependency, nothing to keep updated in-process.

## The three decisions the parent plan asked for

1. **The 48-hour expiry against a retried job.** Cache the provider URI beside the upload
   sidecar with its expiry, and re-upload from our own blob when it is stale. Nothing new
   breaks: our inbox keeps a blob for `keep_hours` (24 h), which is shorter than the
   provider's 48 h, and a job older than that already cannot be retried today.
2. **Does an upload spend the ledger?** Not RPD — an upload is not a `generateContent`
   call, and Google's docs treat it as free and storage-bound. But record it: one row per
   upload with its byte count, so `router_logs` shows it and the 20 GB project budget stays
   observable. Do not invent a quota dimension the provider does not have.
3. **Memory.** The Files API path streams the blob to the provider with no base64 copy, so
   the measured envelope does not change. Skip inline base64: it inflates by a third and
   lands in a buffered request body — the wrong shape for a 350 MB service. If a small-file
   inline path is ever wanted, cap it far below the text cap and measure before trusting it.

## What this needs from the owner

One choice — **A**, **B** or **C** — and one fact that can move the recommendation: are the
PDFs you have in mind text-based documents, or scans and figure-heavy pages? Local
extraction is the right answer for the first and the wrong one for the second.
