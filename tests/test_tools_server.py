"""Tools + server wiring (FR-B1..B9): result shapes, ToolError payloads, SDK v2
registration, call_tool roundtrip, bearer middleware, custom REST routes."""
from __future__ import annotations

import json
import time

import httpx
import pytest
from gemini_router import Router
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from conftest import FakeCtx, FakeRouter, make_app_cfg, make_deps, real_router_cfg
from gemini_mcp.config import AppCfg
from gemini_mcp.ingest import make_ticket
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


async def test_inline_raw_document_rejected_with_fix():
    """Solar-Pro-4-class failure (2026-10-10): raw RTF inlined into a text tool."""
    deps = make_deps()
    rtf = "Here is the file:\n{\\rtf1\\ansi\\ansicpg1251 {\\par garbage}}"
    with pytest.raises(ToolError) as ei:
        await gemini_generate_tool(deps, ProgressEmitter(FakeCtx()), {"prompt": rtf})
    payload = json.loads(str(ei.value))
    assert payload["error"] == "raw_document_inlined" and payload["detected"] == "RTF"
    assert "file_upload_ticket" in payload["fix"] and "analyze_file" in payload["fix"]
    with pytest.raises(ToolError) as ei:
        await analyze_large_tool(deps, ProgressEmitter(FakeCtx()),
                                 {"task": "t", "body": "%PDF-1.7 raw bytes"})
    assert json.loads(str(ei.value))["detected"] == "PDF"
    # guard fires before any routing/quota work: nothing spent, nothing audited
    assert not deps.audit.entries


async def test_quota_status_carries_file_flow_hint(tmp_path):
    router = Router(real_router_cfg(tmp_path))
    deps = make_deps(router=router, app_cfg=make_app_cfg(tmp_path / "logs"))
    q = await quota_status_tool(deps)
    assert "file_upload_ticket" in q["file_flow"] and "analyze_file" in q["file_flow"]


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
                                       "analyze_result", "file_upload_ticket",
                                       "analyze_file", "analyze_files", "quota_status",
                                       "router_logs"}
    af = next(t for t in tools if t.name == "analyze_files")
    af_props = (getattr(af, "inputSchema", None) or af.input_schema)["properties"]
    assert {"task", "upload_ids"} <= set(af_props)
    assert af_props["upload_ids"]["type"] == "array"
    al = next(t for t in tools if t.name == "analyze_large")
    schema = getattr(al, "inputSchema", None) or al.input_schema
    props = schema["properties"]
    for field in ("task", "body", "thinking_level", "overflow", "route",
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


async def test_rest_complete_maps_errors_to_their_own_status(tmp_path):
    """/v1/complete must answer JSON with the status the error class deserves: only
    quota is a 429, everything else is a request the client has to CHANGE."""
    hdr = {"Authorization": "Bearer tok123"}
    deps = make_deps(router=Router(real_router_cfg(tmp_path)),
                     app_cfg=make_app_cfg(tmp_path / "logs"))
    app = build_http_app(build_server(deps), deps.app_cfg, "tok123")
    async with await _client(app) as c:
        r = await c.post("/v1/complete", headers=hdr,
                         json={"prompt": "{\\rtf1\\ansi pasted raw document"})
        assert r.status_code == 400
        assert r.json()["error"] == "raw_document_inlined"      # object, not a string
        assert "analyze_file" in r.json()["fix"]

        r = await c.post("/v1/complete", headers=hdr, json={})
        assert r.status_code == 400 and r.json()["error"] == "missing_prompt"

        r = await c.post("/v1/complete",
                         headers={**hdr, "content-type": "application/json"},
                         content=b"not json")
        assert r.status_code == 400 and r.json()["error"] == "bad_json"

    quota_deps = make_deps(router=FakeRouter(raise_quota=True),
                           app_cfg=make_app_cfg(tmp_path / "logs2"))
    qapp = build_http_app(build_server(quota_deps), quota_deps.app_cfg, "tok123")
    async with await _client(qapp) as c:
        r = await c.post("/v1/complete", headers=hdr, json={"prompt": "hi"})
        assert r.status_code == 429 and r.json()["error"] == "quota_exhausted"
        assert r.json()["per_model"]                            # structured payload kept


def test_inline_doc_guard_only_looks_at_the_head():
    """A document that merely QUOTES a container signature is legitimate text."""
    from gemini_mcp.tools import _inline_doc_guard
    with pytest.raises(ToolError):
        _inline_doc_guard("{\\rtf1\\ansi leading signature", "body")
    quoted = "Notes on file formats.\n\n" + ("filler. " * 900) \
        + "\nA PDF starts with the bytes %PDF-1.7 and an OOXML file with PK."
    _inline_doc_guard(quoted, "body")                           # must not raise


async def test_upload_enforces_the_ticket_size(tmp_path, monkeypatch):
    """The ticket is size-bound, and the check has to happen BEFORE the body is read:
    buffering first would let one oversized upload eat the whole memory budget."""
    monkeypatch.setenv("GEMINI_MCP_TOKEN", "tok123")
    inbox = tmp_path / "inbox"
    cfg = AppCfg.model_validate({"logging": {"dir": str(tmp_path / "logs")},
                                 "ingest": {"dir": str(inbox), "max_file_mb": 1,
                                            "ticket_ttl_s": 600}})
    deps = make_deps(router=Router(real_router_cfg(tmp_path)), app_cfg=cfg)
    app = build_http_app(build_server(deps), cfg, "tok123")
    exp = int(time.time()) + 600

    async def _chunks():
        yield b"z" * 2048                       # chunked → no Content-Length to check

    def url(uid, size):
        return (f"/v1/upload/{uid}?exp={exp}&size={size}"
                f"&sig={make_ticket('tok123', uid, exp, size)}")

    async with await _client(app) as c:
        # A ticket declaring more than the cap is honoured: the cap is enforced where
        # the caller's consent is known — the ticket tool — and the signature is what
        # makes the declaration trustworthy. The route's job is integrity, not policy.
        big = b"x" * (2 * 1_048_576)
        r = await c.put(url("u1", len(big)), content=big)
        assert r.status_code == 200 and r.json()["bytes"] == len(big)

        # Content-Length over the ticket's size → refused before the body is read
        r = await c.put(url("u2", 1024), content=b"x" * 4096)
        assert r.status_code == 413 and r.json()["error"] == "file_too_large"
        assert r.json()["max_bytes"] == 1024

        # chunked (no Content-Length to check) and the wrong length → refused after
        # reading, still nothing stored
        r = await c.put(url("u4", 1024), content=_chunks())
        assert r.status_code == 413 and r.json()["error"] == "size_mismatch"
        assert r.json()["bytes"] == 2048 and r.json()["expected"] == 1024

        # exact match is stored
        r = await c.put(url("u3", 1024), content=b"y" * 1024)
        assert r.status_code == 200 and r.json()["bytes"] == 1024
        assert (inbox / "u3").read_bytes() == b"y" * 1024

    # every accepted upload left its spent-ticket marker; u2 and u4 were refused
    # before anything was written, so they spent nothing
    stored = {p.name for p in inbox.iterdir()}
    assert stored == {"u1", "u1.consumed", "u3", "u3.consumed"}


async def test_the_file_flow_is_stated_once(tmp_path):
    """Both payload hints are the same string. They were four copies, and copies of a
    procedure drift — an agent following a stale one loses a call to a wrong step."""
    from gemini_mcp.tools import FILE_FLOW

    router = Router(real_router_cfg(tmp_path))
    deps = make_deps(router=router, app_cfg=make_app_cfg(tmp_path / "logs"))
    q = await quota_status_tool(deps)
    assert q["file_flow"] == FILE_FLOW

    with pytest.raises(ToolError) as ei:
        await gemini_generate_tool(deps, ProgressEmitter(FakeCtx()),
                                   {"prompt": "{\\rtf1\\ansi inlined"})
    fix = json.loads(str(ei.value))["fix"]
    assert FILE_FLOW in fix                     # the same sentence, not a paraphrase
