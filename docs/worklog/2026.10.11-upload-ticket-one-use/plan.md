# Chunk 1 of `2026.10.11-remaining-work`: item A — an issued upload ticket is never marked consumed

Scope: `gemini-mcp` only. Router untouched. PATCH release (bug fix).

## Problem (as recorded in the parent plan)

The "one-time ticket" guarantee rests on a file-exists check
(`src/gemini_mcp/ingest.py:68`), not on the ticket. The signature is stateless, so
nothing records that a ticket was used: inside its 900 s TTL the same signed URL works
again as soon as the blob is gone. Two edges in the same area: `prune()` runs only on
ticket issuance (an inbox nobody asks tickets from keeps its junk), and the absence of
any consumption record is what blocks a Files API story later (item C).

## Fix

1. **Claim the ticket on use.** `claim_ticket()` creates `inbox/<id>.consumed` with
   `O_CREAT|O_EXCL` — atomic under concurrent PUTs, and it outlives the blob. Order in
   `save_upload`: size → blob-exists (`already_uploaded`, the useful message) → claim
   (authoritative refusal) → atomic write. A crash between claim and write costs a
   re-issued ticket, never a replay.
2. **Refuse early in the route**, before `await request.body()` — a replayed 20 MB PUT
   must not be buffered just to be rejected (the memory guard on a 1 GB VPS).
3. **Second and third prune triggers**: a completed `analyze_file`, and `gemini-mcp
   prune` (which pruned logs and audit rows but not the inbox). `prune()` also ages out
   leftover `.consumed`/`.json`/`.part` files whose blob is already gone.
4. Keep the file-exists check as the second line, not the only one.

## Tests (each fails on the old code)

- replay after the blob is removed → `ticket_consumed`, nothing written.
- `analyze_file` clears a stale inbox it did not issue a ticket for.
- `prune` clears spent markers and orphan `.part` files.
- CLI `prune` reports `inbox_removed`.

## Docs to keep true

`docs/03-architecture.md` §10 (steps 2 and 4: "one-time", and the false "purged after
analysis by default"), `docs/freebuff-wiring.md` (retention line), `docs/02` if needed.

## Publication

Push to `main`, then tag `v0.17.2`: test → PyPI → npm → consumer smoke → VPS deploy.
