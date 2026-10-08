"""Tools + server wiring (FR-B1..B9): result shapes, ToolError payloads, SDK v2
registration, call_tool roundtrip, bearer middleware, custom REST routes."""
from __future__ import annotations

import json

import httpx
import pytest
from gemini_router import Router
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from conftest import FakeCtx, FakeRouter, make_app_cfg, make_deps, real_router_cfg
from gemini_mcp.progress import ProgressEmitter
from gemini_mcp.server import AuthMetadataRoutes, BearerMiddleware, build_http_app, build_server
from gemini_mcp.tools import (
    analyze_large_tool,
    gemini_generate_tool,
    quota_status_tool,
    router_logs_tool,
)


async def test_analyze_large_tool_shape():
    deps = make_deps()
    out = await analyze_large_tool(deps, ProgressEmitter(FakeCtx()),
                                   {"task": "t", "body": "small body"})
    assert out["result"].startswith("SINGLE[")
    assert out["meta"]["requests_spent"] == 1 and out["meta"]["progress_trace"]
    assert deps.audit.entries[0]["tool"] == "analyze_large"


async def test_quota_error_surfaces_as_toolerror_json():
    deps = make_deps(router=FakeRouter(raise_quota=True))
    with pytest.raises(ToolError) as ei:
        await analyze_large_tool(deps, ProgressEmitter(FakeCtx()),
                                 {"task": "t", "body": "small"})
    payload = json.loads(str(ei.value))
    assert payload["error"] == "quota_exhausted"
    assert payload["per_model"] and payload["reset_eta_pt"] and payload["progress_trace"]
    assert deps.audit.entries[0]["error"] == "quota_exhausted"


async def test_gemini_generate_tool():
    deps = make_deps()
    out = await gemini_generate_tool(deps, ProgressEmitter(FakeCtx()),
                                     {"prompt": "hello", "thinking_level": "low"})
    assert out["result"].startswith("SINGLE[")
    meta = out["meta"]
    assert meta["model"] == "gemini-3.8-flash" and meta["quota_after"]
    assert deps.audit.entries[0]["tool"] == "gemini_generate"


async def test_quota_status_and_logs_with_real_router(tmp_path):
    router = Router(real_router_cfg(tmp_path))
    deps = make_deps(router=router, app_cfg=make_app_cfg(tmp_path / "logs"))
    q = await quota_status_tool(deps)
    assert q["recommended_model"] == "gemini-3.8-flash"
    assert "T" in q["reset_eta_pt"]
    assert len(q["rows"]) == 3                      # chain(2) + flash-lite, 1 key

    rid = router.ledger.plan(key_id="ab", model="gemini-3.8-flash", kind="single",
                             est_tokens=10, request_id="r1", source="mcp")
    router.ledger.resolve(rid, "ok", http_status=200, tokens_in=10, tokens_out=4,
                          latency_ms=1200)
    logs = await router_logs_tool(deps, n=10)
    assert logs["calls"][0]["status"] == "ok" and logs["calls"][0]["model"] == \
        "gemini-3.8-flash"


async def test_server_registers_tools_with_expected_schema(tmp_path):
    deps = make_deps(router=Router(real_router_cfg(tmp_path)),
                     app_cfg=make_app_cfg(tmp_path / "logs"))
    srv = build_server(deps)
    tools = await srv.list_tools()
    assert {t.name for t in tools} == {"analyze_large", "gemini_generate",
                                       "analyze_result", "quota_status", "router_logs"}
    al = next(t for t in tools if t.name == "analyze_large")
    schema = getattr(al, "inputSchema", None) or al.input_schema
    props = schema["properties"]
    for field in ("task", "body", "thinking_level", "overflow",
                  "wait_for_minute_limits", "chunk_tokens", "overlap_ratio",
                  "request_id", "wait_seconds"):
        assert field in props
    ar = next(t for t in tools if t.name == "analyze_result")
    ar_schema = getattr(ar, "inputSchema", None) or ar.input_schema
    assert {"request_id", "wait_seconds"} <= set(ar_schema["properties"])
    assert "Analyze a massive text body" in (getattr(al, "description", "") or "")


async def test_call_tool_roundtrip(tmp_path):
    deps = make_deps(router=Router(real_router_cfg(tmp_path)),
                     app_cfg=make_app_cfg(tmp_path / "logs"))
    srv = build_server(deps)
    res = await srv.call_tool("quota_status", {})
    assert not getattr(res, "isError", False)
    payload = getattr(res, "structuredContent", None)
    if payload is None:
        payload = json.loads(res.content[0].text)
    assert "rows" in payload and "recommended_model" in payload


async def test_call_tool_analyze_with_fake_router():
    deps = make_deps(router=FakeRouter(), app_cfg=make_app_cfg())
    srv = build_server(deps)
    res = await srv.call_tool("analyze_large", {"task": "t", "body": "small body"})
    assert not getattr(res, "isError", False)
    payload = getattr(res, "structuredContent", None) or json.loads(res.content[0].text)
    result = payload.get("result") if isinstance(payload, dict) else None
    assert result is not None and result.startswith("SINGLE[")


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_bearer_middleware():
    async def ok(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/healthz", ok),
                            Route("/mcp", ok, methods=["GET", "POST"]),
                            Route("/v1/quota", ok)])
    async with await _client(BearerMiddleware(app, "sekret")) as c:
        assert (await c.get("/healthz")).status_code == 200          # open
        assert (await c.get("/mcp")).status_code == 401
        assert (await c.get("/mcp",
                            headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await c.get("/mcp",
                            headers={"Authorization": "Bearer sekret"})).status_code == 200
        assert (await c.get("/v1/quota",
                            headers={"Authorization": "Bearer sekret"})).status_code == 200


async def test_401_advertises_static_bearer():
    """Freebuff follow-up (v0.2.2): 401 must carry WWW-Authenticate and a JSON body —
    clients that probe unauthenticated decide between OAuth and static token by it."""
    async def ok(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST"])])
    async with await _client(BearerMiddleware(app, "sekret")) as c:
        r = await c.get("/mcp")
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Bearer")
        assert r.json()["error"] == "unauthorized"             # parseable JSON, not text


async def test_auth_metadata_routes():
    """RFC 9728 discovery: protected-resource metadata → static bearer, no AS;
    every other /.well-known/oauth-* → JSON 404 (text/plain crashed Freebuff's parser)."""
    async def ok(request):
        return JSONResponse({"ok": True})

    inner = Starlette(routes=[Route("/healthz", ok)])
    app = AuthMetadataRoutes(inner, "/mcp")
    async with await _client(app) as c:
        r = await c.get("/.well-known/oauth-protected-resource")
        assert r.status_code == 200
        body = r.json()
        assert body["resource"].endswith("/mcp")
        assert body["bearer_methods_supported"] == ["header"]
        r2 = await c.get("/.well-known/oauth-protected-resource/mcp")
        assert r2.status_code == 200 and r2.json()["resource"].endswith("/mcp")
        r3 = await c.get("/.well-known/oauth-authorization-server")
        assert r3.status_code == 404 and r3.json()["error"] == "not_supported"
        r4 = await c.get("/.well-known/openid-configuration")   # OIDC rung of the ladder
        assert r4.status_code == 404 and r4.headers["content-type"].startswith("application/json")
        for p in ("/register", "/authorize", "/token"):          # DCR/auth rungs
            rr = await c.post(p, json={})
            assert rr.status_code == 404 and rr.json()["error"] == "not_supported"
        assert (await c.get("/healthz")).status_code == 200    # passthrough intact


async def test_http_app_custom_routes(tmp_path):
    deps = make_deps(router=Router(real_router_cfg(tmp_path)),
                     app_cfg=make_app_cfg(tmp_path / "logs"))
    app = build_http_app(build_server(deps), deps.app_cfg, "tok123")
    async with await _client(app) as c:
        h = await c.get("/healthz")
        assert h.status_code == 200 and h.json()["ok"] is True
        assert (await c.get("/v1/quota")).status_code == 401
        q = await c.get("/v1/quota", headers={"Authorization": "Bearer tok123"})
        assert q.status_code == 200 and "rows" in q.json()
        assert (await c.post("/mcp", json={})).status_code == 401     # guarded
