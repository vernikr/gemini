# P01 — Freebuff↔gemini-mcp HTTP wiring outage (2026-10-06..08): postmortem

## Symptom
Every HTTP-path connection attempt (native `type: http`, then `mcp-remote` bridge,
with `${VAR}`+env and with literal token) ended in an OAuth "dance": 401 → discovery
ladder → Dynamic Client Registration → fatal client error. Took ~2 days and three
server hardening releases (v0.2.2–v0.2.4) to resolve.

## Root cause (single)
The bearer token in the user's `~/.agents/mcp.json` was **65 characters instead of
64** — one stray character introduced while hand-copying from terminal output. The
server correctly answered 401 to a wrong token; spec-compliant clients correctly fell
back to OAuth discovery and died there. Every layer behaved correctly; only the
configuration was corrupt.

## Why it took so long (contributing factors, ordered by cost)
1. **Debugging method (agent side — main time cost).** Hypotheses were built and
   "fixed" at the TOP of the stack (Freebuff drops headers → mcp-remote stale auth
   cache → discovery poison) while the BOTTOM assumption (the token bytes on the
   wire are correct) was never measured. The decisive probe — one `curl -w
   '%{http_code}'` POST with the exact configured token — was run only at the very
   end and answered in 30 seconds what theories could not in two days.
2. **Green sandbox repro misread as exoneration.** A clean-environment repro
   connected successfully, proving code health; it was treated as proof that the
   user's machine "state" was broken. The real difference (token text) was inside
   the user's config all along and was never diffed byte-wise.
3. **Human transcription of secrets.** The setup flow required the user to copy a
   64-hex token by hand between terminal and JSON — an error-prone step baked into
   the design. Fixed: token now travels via `ssh ... | pbcopy` / programmatic
   mcp.json rewrite, never by hand.
4. **Client call timeout (~60 s) vs thinking-heavy jobs (minutes).** Genuine
   ecosystem constraint (not a bug): first successful connection was followed by a
   "Connection closed" on a long analyze. Fixed properly in v0.3.0: async jobs
   (wait_seconds → running-ticket → analyze_result polling).
5. **Free-tier 503 storms.** Google free tier overloaded (3.8/3.7 → 503, 3.6
   answered) making runs slow; informed v0.4.0 routes ('optimal' = availability
   order first, auto lite/gemma overflow).
6. **Server etiquette gaps (real but secondary).** text/plain 404s on OAuth
   discovery rungs crashed client JSON parsers; hardened in v0.2.2–v0.2.4 (RFC 9728
   metadata, JSON-only on every `/.well-known/*` + `/register,/authorize,/token`).
   Good hygiene, but it treated symptoms while the cause was the token.

## Verdict per side
- **User/config**: root cause (corrupted token) — but caused by a process that
  demanded manual secret transcription.
- **Agent (me)**: main duration cost — theorizing before boundary measurement.
- **Freebuff**: innocent on headers/auth; real constraint = call timeout.
- **Google**: weather (503s), expected on free tier, handled by design.
- **Server code**: behaved correctly throughout; etiquette hardening was optional.

## Generalized rules (added to global AGENTS.md guidance)
1. Boundary probe before theory: measure at each layer in the FAILING environment
   (exact configured credentials, `curl -w '%{http_code}'`) before any hypothesis.
2. Secrets never travel by hand: pipe/script them end-to-end and verify
   (length/hash). Manual transcription is a design defect.
3. A green repro is not a diagnosis: enumerate repro↔real differences and probe
   each difference on the real machine.
4. Batch discriminating probes when user round-trips are expensive: several checks
   with expected outputs in one message, not one hypothesis per turn.
5. Design for hostile clients: assume ~30–60 s call timeouts (async ticket + poll
   for anything longer), JSON-only error bodies on every endpoint a client may
   parse, and never text/plain on discovery/auth rungs.
