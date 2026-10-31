# AGENTS.md — gemini

Follow the owner's global AGENTS.md: write repository files in English, discuss in Russian,
use Conventional Commits and SemVer, and update documentation with behavior changes.

## Context
- `docs/02-requirements.md` and `docs/03-architecture.md` define the gateway contract and
  design. Keep them aligned with the implementation.
- The gateway owns MCP tools, file ingest, chunking, logging, and backend selection.
  `gemini-router` owns provider routing, quota accounting, and its shared SQLite ledger.
- Do not hard-code a private host, user path, credential, or infrastructure detail. Avoid
  unrelated network, firewall, or host-service changes. Install Python tooling with `uv`.
- The npm launcher checks PyPI for a newer gateway core. `--no-update` and
  `GEMINI_AUTO_UPDATE=0` disable the check; when the registry is unavailable it uses the
  version paired with the launcher.
- TTS terminal progress is based on completed synthesis chunks and MP3 encoding, not time
  estimates. JSON output remains machine-readable.

## Commands
- `uv sync --extra dev` · `uv run pytest` · `uv run ruff check src tests`
- `uv run gemini stdio|serve|setup|uninstall|quota|calls|logs|prune|doctor|smoke|tts`
- `npm test --prefix packaging/npm`
- Fleet deployment helpers require an explicit `VPS_HOST` and approved SSH access.

## Secrets and testing
- Never commit `.env`, keys, tokens, real request bodies, private keys, or user credentials.
  Show only key fingerprints (`key_id = sha256[:8]`) in diagnostics.
- Runtime credentials belong in protected user or service configuration; data and ledger
  paths are deployment configuration, not source constants.
- CI uses mocked provider responses and does not spend live quota. Run the secret guard
  before release; test credential-shaped fixtures must carry the `DUMMY` marker.

## Documentation and releases
- Evergreen documentation describes current interfaces and behavior. Release notes belong
  in package changelogs; avoid implementation diaries, migration provenance, stale dates,
  duplicated version numbers, and test-count claims.
- Python and npm artifact versions stay aligned. Release workflows verify the package/tag
  lockstep before publishing.
- For native GitHub sub-issues, use `.github/workflows/issue-parent-sync.yml` and add
  `<!-- issue-parent: #N -->` to the child Issue body; use `<!-- issue-parent: none -->`
  to detach it.
