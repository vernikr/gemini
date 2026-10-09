"""Direct mode (0.8.0): MemLedger contract parity with sqlite Ledger, backend
resolution (auto/direct/router + env), and the Router engine running on MemLedger
against a mocked AI Studio (respx) — same protocol, no disk state."""
from __future__ import annotations

import json
import time

import httpx
import pytest
import respx
from gemini_router import Router, RouterRequest

from gemini_mcp.backends import MemLedger, make_routers, resolve_mode
from gemini_mcp.config import AppCfg
from gemini_mcp.progress import ProgressEmitter
from gemini_mcp.tools import gemini_generate_tool

BASE = "https://generativelanguage.googleapis.com/v1beta"


def _cfg(tmp_path, keys=("k1",), **over):
    from gemini_router.config import Config
    data = {"api_keys": list(keys), "chain": ["gemini-3.8-flash", "gemini-3.7-flash"],
            "state_path": str(tmp_path / "state.db"),
            "models_refresh_h": 0}
    data.update(over)
    return Config.model_validate(data)


# ---------------------------------------------------------------- MemLedger
def test_memledger_plan_resolve_and_rpd():
    led = MemLedger()
    now = time.time()
    rid = led.plan(key_id="ab", model="m", kind="single", est_tokens=10,
                   request_id="r1", source="mcp", now=now)
    # planned-but-fresh counts toward the minute window, not toward RPD
    assert led.rpd_spent("ab", "m", now - 100, now - 200) == (0, 0)
    calls, toks, oldest = led.window_usage("ab", "m", now - 60, now - 300)
    assert (calls, toks) == (1, 10) and oldest == now
    led.resolve(rid, "ok", http_status=200, tokens_in=12, tokens_out=4, latency_ms=900)
    assert led.rpd_spent("ab", "m", now - 100, now - 200) == (1, 1)
    row = led.calls_tail(10)[0]
    assert row["status"] == "ok" and row["tokens_in"] == 12 and row["http_status"] == 200
    # count rows never spend RPD
    cid = led.plan(key_id="ab", model="m", kind="count", est_tokens=5,
                   request_id=None, source="count", now=now)
    led.resolve(cid, "ok")
    assert led.rpd_spent("ab", "m", now - 100, now - 200) == (1, 1)


def test_memledger_cooldown_cache_prune_stats():
    led = MemLedger()
    now = time.time()
    led.set_cooldown("ab", "m", now + 30, "quota_day")
    led.set_cooldown("ab", "m", now + 10, "weaker")       # MAX semantics
    assert led.cooldown("ab", "m", now) == pytest.approx(now + 30)
    led.cache_put("h1", "r1", "m", "RESP", 3, 4, now=now)
    hit = led.cache_get("h1", None, 3600, now=now + 5)
    assert hit["response"] == "RESP" and hit["request_id"] == "r1"
    assert led.cache_get("h1", None, 1, now=now + 50) is None      # TTL
    assert led.cache_get(None, "r1", 3600, now=now + 5)["model"] == "m"
    out = led.prune(audit_keep_days=30, cache_ttl_s=1, now=now + 50)
    assert out["cache_pruned"] == 1
    assert led.stats() == {"calls": 0, "cache": 0, "size_mb": 0.0, "storage": "memory"}
    assert led.path == ""                                          # never on disk


# ---------------------------------------------------------------- mode select
def test_resolve_mode_auto_direct_router_and_env(tmp_path, monkeypatch):
    app = AppCfg()
    cfg = _cfg(tmp_path)
    monkeypatch.delenv("GEMINI_MCP_BACKEND", raising=False)
    monkeypatch.delenv("GEMINI_ROUTER_CONFIG", raising=False)
    assert resolve_mode(app, cfg) == "direct"                 # no state → consumer
    (tmp_path / "state.db").write_bytes(b"x")
    assert resolve_mode(app, cfg) == "router"                 # fleet state exists
    (tmp_path / "state.db").unlink()
    monkeypatch.setenv("GEMINI_MCP_BACKEND", "router")
    assert resolve_mode(app, cfg) == "router"                 # explicit env wins
    monkeypatch.setenv("GEMINI_MCP_BACKEND", "direct")
    (tmp_path / "state.db").write_bytes(b"x")
    assert resolve_mode(app, cfg) == "direct"
    monkeypatch.setenv("GEMINI_MCP_BACKEND", "bogus")
    with pytest.raises(ValueError):
        resolve_mode(app, cfg)


def test_make_routers_direct_persists_local_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_MCP_HOME", str(tmp_path / "gmcp"))
    monkeypatch.delenv("GEMINI_MCP_BACKEND", raising=False)
    monkeypatch.delenv("GEMINI_ROUTER_CONFIG", raising=False)
    monkeypatch.delenv("GEMINI_ROUTER_STATE", raising=False)
    monkeypatch.chdir(tmp_path)
    app = AppCfg()
    cfg = _cfg(tmp_path, state_path="state.db")     # default → maps into config home
    router, optimal, mode = make_routers(app, cfg)
    assert mode == "direct"
    state = tmp_path / "gmcp" / "state.db"
    assert router.ledger.path == str(state) and state.exists()
    assert optimal is not None and optimal.ledger.path == str(state)   # one shared file
    assert optimal.cfg.chain == list(app.routing.optimal_chain)
    from gemini_router.config import key_id as _kid
    rid = router.ledger.plan(key_id=_kid("k1"), model="gemini-3.8-flash", kind="single",
                             est_tokens=5, request_id=None, source="mcp")
    router.ledger.resolve(rid, "ok", tokens_in=5, tokens_out=2)
    router2, _, _ = make_routers(app, cfg)                           # simulate restart
    left = {r["model"]: r["rpd_left"] for r in router2.model_budgets()}
    assert left["gemini-3.8-flash"] == 19                            # spend survived
    assert left["gemini-3.7-flash"] == 20


# ---------------------------------------------------------------- E2E (respx)
def _mock_models(router_mock):
    router_mock.get(f"{BASE}/models").mock(return_value=httpx.Response(200, json={
        "models": [{"name": "models/gemini-3.8-flash"}, {"name": "models/gemini-3.7-flash"}]}))


def _gen_body(text="OK", tin=5, tout=2):
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": tin, "candidatesTokenCount": tout}}


@pytest.mark.asyncio
async def test_direct_complete_happy_path_and_quota_decrement(tmp_path):
    with respx.mock(assert_all_called=False) as rx:
        _mock_models(rx)
        g = rx.post(f"{BASE}/models/gemini-3.8-flash:generateContent").mock(
            return_value=httpx.Response(200, json=_gen_body("HELLO")))
        router = Router(_cfg(tmp_path), ledger=MemLedger())
        res = await router.complete(RouterRequest(prompt="hi", source="mcp"))
        assert res.text == "HELLO" and res.model == "gemini-3.8-flash"
        assert res.tokens_in == 5 and res.tokens_out == 2 and g.called
        budgets = router.model_budgets()
        assert budgets[0]["rpd_left"] == 19                    # 20 - 1 spent
        assert router.ledger.calls_tail(5)[0]["status"] == "ok"
        await router.aclose()


@pytest.mark.asyncio
async def test_direct_429_perday_switches_model(tmp_path):
    with respx.mock(assert_all_called=False) as rx:
        _mock_models(rx)
        rx.post(f"{BASE}/models/gemini-3.8-flash:generateContent").mock(
            return_value=httpx.Response(429, json={
                "error": {"message": "You exceeded your current quota (PerDay per model)"}}))
        g2 = rx.post(f"{BASE}/models/gemini-3.7-flash:generateContent").mock(
            return_value=httpx.Response(200, json=_gen_body("SECOND")))
        router = Router(_cfg(tmp_path), ledger=MemLedger())
        res = await router.complete(RouterRequest(prompt="hi", wait=False))
        assert res.model == "gemini-3.7-flash" and res.text == "SECOND" and g2.called
        await router.aclose()


@pytest.mark.asyncio
async def test_direct_count_tokens(tmp_path):
    with respx.mock(assert_all_called=False) as rx:
        _mock_models(rx)
        rx.post(f"{BASE}/models/gemini-3.8-flash:countTokens").mock(
            return_value=httpx.Response(200, json={"totalTokens": 1234}))
        router = Router(_cfg(tmp_path), ledger=MemLedger())
        assert await router.count_tokens("some text") == 1234
        # countTokens must not spend RPD
        assert router.model_budgets()[0]["rpd_left"] == 20
        await router.aclose()


@pytest.mark.asyncio
async def test_direct_report_and_meta_carry_backend(tmp_path):
    import logging

    from gemini_mcp.logging_setup import AuditLogger
    from gemini_mcp.tools import Deps
    with respx.mock(assert_all_called=False) as rx:
        _mock_models(rx)
        rx.post(f"{BASE}/models/gemini-3.8-flash:generateContent").mock(
            return_value=httpx.Response(200, json=_gen_body("R")))
        app = AppCfg.model_validate({"logging": {"dir": str(tmp_path / "logs")}})
        router = Router(_cfg(tmp_path), ledger=MemLedger())
        deps = Deps(router=router, app_cfg=app,
                    audit=AuditLogger(app.logging), logger=logging.getLogger("t"),
                    backend="direct")
        out = await gemini_generate_tool(deps, ProgressEmitter(None), {"prompt": "x"})
        assert out["report"].startswith("backend=direct ·")
        assert out["meta"]["backend"] == "direct"
        assert json.loads(json.dumps(out, default=str))       # serializable
        await router.aclose()
