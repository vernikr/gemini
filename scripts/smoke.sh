#!/usr/bin/env bash
# smoke.sh — first real-key verification ON THE SERVER (after setup_server.sh).
# Budget: doctor/listModels + countTokens are FREE (kind='count' never spends);
# exactly ONE tiny generate call → spends 1 RPD of the first chain model.
set -euo pipefail
APP=/opt/apps/gemini-mcp
BIN="$APP/.venv/bin"
set -a; . "$APP/.env"; set +a
cd "$APP"

echo "== 1/4 doctor (online: listModels → exact chain IDs resolve) =="
"$BIN/gemini-mcp" doctor

echo
echo "== 2/4 countTokens (free pre-flight, ledger kind='count') =="
"$BIN/python" - <<'PY'
import asyncio
from gemini_router import Config, Router
async def main():
    r = Router(Config.load())
    n = await r.count_tokens("Hello world! " * 40)
    print("count_tokens →", n)
    await r.client.aclose()
asyncio.run(main())
PY

echo
echo "== 3/4 one tiny generate (1 RPD; thinking low to keep it cheap) =="
"$BIN/python" - <<'PY'
from gemini_router import Config, Router, RouterRequest
r = Router(Config.load())
res = r.complete_sync(RouterRequest(
    prompt="Reply with exactly one word: OK",
    kind="single", source="smoke", thinking_level="low", max_output_tokens=16))
print(f"model={res.model} key={res.key_id} cached={res.cached} "
      f"tokens_in={res.tokens_in} tokens_out={res.tokens_out} text={res.text!r}")
PY

echo
echo "== 4/4 quota table (spend must show 1 RPD used on the chosen model) =="
"$BIN/gemini-mcp" quota
echo
echo "SMOKE OK. If step 3 printed text='OK' and step 4 shows 19/20 — the server is live."
