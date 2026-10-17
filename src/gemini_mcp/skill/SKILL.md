---
name: gemini
description: Use when analysing documents or asking Gemini through the gemini gateway or CLI — picking between analyze_large, analyze_file and the file-upload ticket, reading the report line, relaying ⚠️ and ⛔️ badges to the user, handling free-tier quota exhaustion, and staying inside the 20 MB upload limit.
---

# gemini: how to drive the gateway

This skill carries the *behavioural* policy. The mechanics of each call — parameters,
response shape, error codes — ride in the tool descriptions and `--help`; nothing here
restates them.

## Which tool

- `analyze_large` takes the body as an argument, so the text passes through your
  context on the way. Use it only when the text is already there.
- When it is not — it sits in a file, or it is long enough that inlining it would cost
  you the window — do **not** paste it. Write it to a file and go through
  `file_upload_ticket` + `analyze_file`. Text documents are extracted server-side and
  run the same chunked pipeline, so nothing is lost and the bytes never enter your
  context.
- CLI equivalent, one step: `gemini analyze --body-file <path>`, or the body on
  stdin.
- Never pin `model` unless the user asked for one. The chain exists to keep working
  when a model's budget or availability changes, and pinned ids go stale.

## Routing profiles

The `route` argument of the compute tools:

- `optimal` (default) — models are tried in empirical availability order, older first,
  because older models are less loaded on the free tier. Overflow to flash-lite/gemma
  is **automatic** when Flash budgets run out.
- `best_available` — newest-first chain; overflow is strictly opt-in, so `off` is a
  hard stop on quota exhaustion.

Call `quota_status` **before** big jobs; `router_logs` shows recent spend.

## Length

{brevity_rule}

## Badges — what the user must see

Relay ⚠️ alongside **each** unrequested flash-lite/gemma model appearing anywhere in
the answer's `report` (a job may use several models, including a stronger one for final
consolidation). For any degraded outcome — waits, retries, missing chunks, split PDF,
cost drift, unexpected weaker model — show ⚠️ and explain the reason, not just the
symbol. For a failed tool call or an answer that could not be produced at all, prefix
your user-facing message with ⛔️ and explain the recovery step. Never mark a fully
successful clean run as an error.

## The report line

Every `analyze_large` / `gemini_generate` / `analyze_file` response starts with a
one-line `report` field: route · thinking level · final model · requests spent ·
"clean run" or a rough-edges count (distinct wait/retry/switch **episodes**, not a
failed-request count). **Always** relay that line to the user in chat. If
`rough_edges > 0`, say in plain words what happened — provider 503s and retries, model
fallbacks, minute-limit waits with seconds, failed chunks; the detail is in
`meta.progress_trace`. If the call errored, say so plainly with the reset ETA. Never
silently swallow retries, waits or fallbacks.

## Quota exhaustion

On a hard stop you get a structured `quota_exhausted` / `plan_budget_insufficient`
error carrying per-model budgets, wait times and reset ETA. Then retry later, shrink
the job, or opt in with `overflow='lite'` / `'lite+gemma'`. Minute-level limits are
worth waiting out — the server already does by default (`wait_for_minute_limits`).
Daily quota exhausted across every key is the ⛔️ case: tell the user, do not retry.

## The 20 MB limit

That cap is a **memory** limit, not a policy: the server holds the extracted text in
RAM while chunking, so an oversized file can push the service past its memory allowance
and kill every job in flight, not just yours. When what you need to send adds up to
more, work down this list:

1. **Shrink it locally** — re-save as plain text or Markdown. RTF and DOCX carry markup
   that often outweighs the words, and a `.txt`/`.md` export reads just as well.
   Archives are not accepted.
2. **Split it** — several uploads and several `analyze_file` calls, then combine the
   answers yourself. Each call is charged separately; say so in your report.
3. **Only if neither works**, ask the user — in this chat, before you act — for
   explicit permission to exceed the cap. Tell them plainly that the service may be
   killed mid-job, taking other people's unfinished work with it, and how far over you
   need to go. If and only if they say yes, retry with `override_size_limit=true`.
   Their consent is the only gate: nothing on the server will stop you, so do not treat
   a vague "sure, try it" about something else as permission for this.

Do not paste a large file into `body` to get around this: `analyze_large` rejects raw
documents, and a huge paste costs the same memory without the ticket's protections.
