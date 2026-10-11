# Incident: two production API keys committed into a public test fixture

**Severity:** one key revoked by the vendor as leaked ("reported as leaked", HTTP 403 on
every endpoint); the second stopped authenticating (HTTP 401). No unauthorised use was
observed; the window between the commit and the vendor's flag was under an hour.

## Timeline (UTC, 2026-10-11)

| time | event |
|---|---|
| ~11:45 | commit `4231254` (`feat(cli): gemini-mcp keys …`) pushed to `main` of the public repo `vernikr/gemini-mcp`; `tests/test_keys_cmd.py` contains two real keys as test constants |
| ~12:34 | tag `v0.20.0` released; the sdist on PyPI carries `tests/`, so the keys became downloadable from PyPI as well |
| ~12:45 | the daemon's `analyze_file` failed with `file_upload_failed`; probes showed key 1 → **403 "Your API key was reported as leaked. Please use another API key."**, key 2 → **401 unauthenticated** |

## Cause

Writing test fixtures, the two live keys from the working context (the daemon's pool and the
spare account key the owner had pasted) were copied verbatim into `tests/test_keys_cmd.py`
instead of being replaced by obviously fake values. `ci.yml` lints `src` and `tests` but has
no secret scan; nothing else looked at file contents, so the commit went straight through.

## Blast radius

- Public repo history: commits `4231254`, `38b5dc6`, `0552ebd`, tags `v0.19.0`/`v0.20.0`.
- PyPI sdists `0.19.0` and `0.20.0` (sdist includes `tests/`). Wheels are clean.
- Both keys are dead; the pool is empty until a new key is added. Out-of-band copies are
  inert, and PyPI files cannot be edited (nor need to be — the credentials they contain are
  revoked).

## Remediation (this repo)

- Fixtures now use DUMMY-marked fakes; existing unmarked fakes in `test_logging.py` were
  marked at the same time.
- New rule + enforcement: `gemini_mcp/secrets_guard.py` (credential-shaped literals —
  `AIza…`, `AQ.…`, `sk-…`, `ghp_…`, `AKIA…`, `xox…` — must contain the marker `DUMMY`),
  entry point `scripts/check_secrets.py`, wired as the FIRST step of both `ci.yml` and
  `release.yml` so a leak stops a tag before anything is published.
- Tests pin both directions: a real-shaped literal is flagged, a DUMMY-marked one is not,
  and the repository scan must be clean (167 tests).
- `AGENTS.md` states the rule where an agent reads it before touching fixtures.

## Outstanding

- **Rotation is the owner's to do**: create a fresh key (AI Studio currently issues `AQ.`
  keys; Cloud Console still issues `AIza…` ones) and add it with `gemini-mcp keys add` on
  the host that runs the daemon — the command validates, probes free and restarts the unit.
- Decide whether to rewrite public history. Not urgent: both credentials are revoked and the
  PyPI copies cannot be changed; rewriting would force-push tags whose artifacts are already
  consumed (VPS deploy, npm, PyPI), so the recommendation is to rotate and leave history.
- The AQ key deserves a note: it worked at ~11:07 and was rejected at ~12:45 on every
  transport (header, query, bearer, v1alpha). Whether it was revoked with the same alert or
  hit the known `AQ.` 401 issue is for the account owner to see in AI Studio.
