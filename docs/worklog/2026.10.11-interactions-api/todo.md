# TODO: migrate from `generateContent` to the Interactions API

Raised 2026-10-11 by the owner. Not scheduled — this file exists so the decision is made
once, on verified facts, when it is worth making.

## Verified state of the world (ai.google.dev, 2026-10-11)

- Interactions API is **Generally Available since June 2026** and recommended for all new
  projects. `generateContent` is called **legacy** but remains fully supported, and a
  migration guide exists.
- New models, multimodal capabilities, tools and agent features launch **on Interactions
  first**. That is the strategic reason to move, not any single feature.
- Server-side state: the default `store=true` keeps the interaction and lets the next call
  reference it with `previous_interaction_id`. Retention: **paid 55 days, free tier 1 day**.
  `store=false` opts out, but is **incompatible with background execution** and with
  chaining.
- Per-turn parameters (`system_instruction`, `tools`, `generation_config` including
  `thinking_level`) are interaction-scoped and must be re-sent every turn even in stateful
  mode — relevant to how our gateway builds requests.
- **Background execution** (`background=true`) for long-running tasks — the feature that
  actually matters for a gateway whose jobs outlive a client's patience.
- Not yet available in Interactions (we use none of them today, but they gate some future
  plans): Batch API, **explicit caching**, custom safety settings, remote MCP for Gemini 3.
  Implicit caching works and is what stateful conversations benefit from.
- Model support covers our chains as they stand (`gemini-3.8-flash`, `3.6-flash`,
  `3.5-flash-lite`, `3-flash-preview`, `gemma-4-31b-it`).

## What applies to THIS project, honestly

The gateway is **single-shot and stateless by design**: every chunk, consolidation and
single call is an independent prompt with no chat history, and the model never calls our
MCP tools. So:

| Claimed win | For us |
|---|---|
| Client stops resending history, fewer input tokens | **Does not apply** — we never resend history; there is none |
| Higher implicit cache hit rates | **Does not apply** — our own content-hash cache already does this job, and we control its TTL |
| Automatic function-calling loop | **Does not apply** — we do not use function calling; our tools run on our server |
| Fewer retry/recovery paths | Marginal — we already have the ledger, the taxonomy and the ticket pattern |
| **Background execution** | **Applies.** Today a long job survives a client disconnect through our own running-ticket polling; `background=true` may replace hand-rolled machinery — worth a spike |
| **New features launch there first** | **Applies.** The longer we wait, the more capability we forgo |

## Prerequisites before scheduling

1. A spike on the router client: does `countTokens`, file references and the error taxonomy
   map onto the Interactions shapes without losing the ledger's per-model truth? The
   ledger is our quota authority and must keep counting the same way.
2. A decision on `store`: our retry-ready design wants statelessness (`store=false`), and
   free-tier retention of 1 day is shorter than our own 24-hour inbox — but `store=false`
   forbids background execution. The two features may not be combinable for us.
3. Free-tier availability of `background=true` and of file references — verify live, do not
   assume, and record the finding here.

## Trigger to schedule

- A feature we want ships on Interactions only, **or**
- background execution removes a real piece of our own machinery, **or**
- Google starts marking `generateContent` deprecated rather than legacy.
