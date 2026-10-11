# Item A closed: an upload ticket is spent on first use

Released as **0.17.2** (gemini-mcp only; the router was not touched). Full pipeline green:
test → PyPI → npm → VPS deploy → consumer smoke. PyPI and npm both serve 0.17.2, and the
daemon on `YOUR_VPS_HOST` reports 0.17.2 with `gemini-mcp.service` active since 01:28 UTC.

## What was actually wrong

The "one-time ticket" promise rested on a file-exists check. The HMAC signature is
stateless and nothing recorded a use, so inside the 900 s TTL the same signed URL worked
again as soon as the blob was gone — pruned, evicted by the total cap, or removed by
hand. The real guarantee was "one file per ticket", not "one use per ticket".

## What changed

- `inbox/<id>.consumed`, created with `O_EXCL` before the bytes land: atomic between
  concurrent PUTs and outliving the blob. A crash between the claim and the write costs a
  re-issued ticket, never a replay window.
- The route refuses a spent ticket **before** `await request.body()`: a replayed 20 MB PUT
  must not be buffered just to be rejected.
- Cap eviction drops the bytes and keeps the marker — freeing disk must not hand a ticket
  back inside its TTL. The age path (past `keep_hours`, many TTLs later) drops it too.
- Prune gained two triggers besides ticket issuance: the start of every `analyze_file`,
  and `gemini-mcp prune`, which covered logs and audit rows while the inbox — the one
  place files accumulate — waited for a ticket that might never come. Leftover markers,
  sidecars and `.part` files now age out on the same clock.
- The blob-exists check stays as the second line, where it can still name the useful next
  call (`already_uploaded` → `analyze_file`).

## Evidence

- 121 tests pass, lint clean; `gemini-mcp prune` reports `inbox_removed` (`null` under
  `--dry`, never a misleading `0`).
- Every new test fails on the old code (checked by stashing `src/` and rerunning):
  replay-after-removal at the route and at `save_upload`, the `analyze_file` trigger, the
  marker/part sweep, the CLI count, and the eviction/age distinction.
- **Live probe on the production daemon**, no Gemini spend: a genuine signed ticket
  accepted the first PUT (200), the blob was removed, the replay answered
  `409 ticket_consumed` with only the `.consumed` marker left in the inbox, and a third
  attempt recreated nothing. Scratch files cleaned afterwards.

## Docs

- `docs/02-requirements.md`: new **FR-E9** states the inbox guarantee and all three
  triggers; the six new tests name it.
- `docs/03-architecture.md` §10: step 2 documents the claim, step 4 corrected — it claimed
  the blob is "purged after analysis by default", which the code never did (blobs live
  until `keep_hours`, and re-analyzing the same `upload_id` is a supported second call).
- `docs/freebuff-wiring.md`: retention line now names all three triggers and the one-use
  rule — a failed upload means a fresh ticket, never a retry of the old URL.
- Three stale `§9` references in `server.py`, `ingest.py`, `config.py` fixed to `§10`
  (file ingest is section 10).

## Notes for the next reader

- Item C (Files API) is unblocked on the ledger side: a ticket now has a durable record of
  use, which is what a clean Files API story needs for "this upload was charged".
- `ci.yml` still runs on tags, so every release runs `ci.test` and `release.test` over the
  same commit — that is item B, untouched here.
- The live probe recipe (extract the key from `owner deployment notes`, `ssh -i`, hand-build a
  signed ticket, PUT/replay/clean) is in this report; it is not yet an evergreen doc.
