# Probes behind report §0.18.2

Run on the production VPS (stdlib only; the key is read from the daemon's
`/opt/apps/gemini-mcp/.env` and never printed).

- `vps_tokens.py <files…>` — uploads each file, waits for ACTIVE, prints `countTokens` per
  file together with pixel size and byte size; then times a short question against the
  first file at the default and at `thinking=low`, printing `usageMetadata` (including
  thinking tokens).
- `probe_lib.py` — shared helpers (`upload`, `count`, `ask`), including the
  `media_resolution` part field; import-only.
- `vps_probe3.py` — the decisive run: fresh upload answered immediately (low vs default
  thinking), the `media_resolution` sweep on one image, and the dimension extremes.

Findings and tables: `../report.md` §0.18.2. Reminder recorded there: the serving key is
free tier (20 generate requests per model per day) — counting is free, generating is not.
