# Local TTS CLI — product integration

## Goal

Expose the existing `gemini-router` TTS engine as `gemini tts <file.txt|file.md>` in the user-facing `gemini` CLI. Keep `gemini` as the setup/consumer layer and `gemini-router` as the routing engine.

## Shipped in this change

- Added `gemini tts` with output path, voice, style, bitrate, wait policy, backend override, VPS target override and JSON result options.
- Direct mode uses local keys and the local ledger. Router mode uses the VPS proxy and shared key pool even if local upstream keys are present.
- Router setup stores the VPS host/user in `~/.config/gemini/fleet.json` (0600); uninstall removes it. No token is saved or intentionally emitted.
- Router mode reads the proxy token through SSH into process memory, opens a temporary `127.0.0.1`-only forward, checks proxy health and closes the SSH process on completion/failure.
- Updated requirements, consumer guide, dual-mode architecture, README, dependency floor (`gemini-router>=0.13,<0.14`) and Python/npm versions to `0.27.0`.

## Boundaries

- No new TTS engine, public VPS port, service, or notification dependency.
- Only UTF-8 `.txt`/`.md`, one speaker, non-streaming, one mono MP3; voice/style optional; 128 kbps default.
- Router TTS preflights the complete input before generating, uses the router's paragraph/sentence chunker and quota gates, and removes partial output on failure. Completed output is local and left at the selected path for delivery recovery.
- The local machine needs `ffmpeg`. The proxy token is never written to the profile.

## Validation

- `uv run pytest`: 250 passed.
- `uv run ruff check .`: passed; new TTS modules and their tests pass `ruff format --check`.
- `uv build`: wheel and sdist `0.27.0` built.
- `python scripts/check_secrets.py`, `git diff --check`, Node syntax check and npm dry-run package check: passed.
- SSH/token/tunnel behavior is mocked in tests; a live Mac-to-VPS tunnel was not verified in this work session.
