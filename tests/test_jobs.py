"""Async ticket flow (jobs.py + _jobbed + analyze_result): inline fast path,
running-ticket on slow jobs, fetch-until-done, dedupe by request_id, error
propagation, unknown-id hint. No network (b01)."""
from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import FakeCtx, FakeRouter, make_deps
from gemini_mcp.progress import ProgressEmitter
from gemini_mcp.server import build_server
from gemini_mcp.tools import _jobbed, analyze_large_tool, analyze_result_tool


class SlowRouter(FakeRouter):
    def __init__(self, delay: float = 0.3, **kw):
        super().__init__(**kw)
        self.delay = delay

    async def complete(self, req, on_event=None):
        await asyncio.sleep(self.delay)
        return await super().complete(req, on_event)


def _payload(res):
    return getattr(res, "structuredContent", None) or json.loads(res.content[0].text)


async def test_fast_job_returns_inline():
    deps = make_deps(router=FakeRouter())
    out = await _jobbed(deps, "analyze_large", ProgressEmitter(FakeCtx()),
                        {"task": "t", "body": "small"}, 25,
                        lambda em, pr: analyze_large_tool(deps, em, pr))
    assert out["result"].startswith("SINGLE[") and "status" not in out
    assert any(e.get("event") == "async_submitted" for e in deps.audit.entries)


async def test_slow_job_tickets_then_completes_via_server():
    deps = make_deps(router=SlowRouter(0.3))
    srv = build_server(deps)
    res = await srv.call_tool("analyze_large",
                              {"task": "t", "body": "small", "wait_seconds": 0})
    ticket = _payload(res)
    assert ticket["status"] == "running" and ticket["request_id"]
    res2 = await srv.call_tool("analyze_result",
                               {"request_id": ticket["request_id"], "wait_seconds": 2})
    out = _payload(res2)
    assert out["result"].startswith("SINGLE[") and "meta" in out


async def test_same_request_id_runs_job_once():
    router = SlowRouter(0.2)
    deps = make_deps(router=router)
    run = lambda em, pr: analyze_large_tool(deps, em, pr)  # noqa: E731
    params = {"task": "t", "body": "small", "request_id": "dedupe-1"}
    a = await _jobbed(deps, "analyze_large", ProgressEmitter(None), params, 0, run)
    b = await _jobbed(deps, "analyze_large", ProgressEmitter(None), params, 0, run)
    assert a["status"] == b["status"] == "running"
    out = await analyze_result_tool(deps, "dedupe-1", 2)
    assert out["result"].startswith("SINGLE[")
    assert len(router.calls) == 1


async def test_unknown_request_id_gets_hint():
    deps = make_deps()
    out = await analyze_result_tool(deps, "never-submitted", 0)
    assert out["status"] == "unknown" and "SAME request_id" in out["hint"]


async def test_background_quota_error_propagates_on_fetch():
    deps = make_deps(router=SlowRouter(0.05, raise_quota=True))
    ticket = await _jobbed(deps, "analyze_large", ProgressEmitter(None),
                           {"task": "t", "body": "small", "request_id": "q-1"},
                           0, lambda em, pr: analyze_large_tool(deps, em, pr))
    assert ticket["status"] == "running"
    with pytest.raises(ToolError) as ei:
        await analyze_result_tool(deps, "q-1", 2)
    assert "quota_exhausted" in str(ei.value)


async def test_inline_quota_error_still_raises_immediately():
    deps = make_deps(router=FakeRouter(raise_quota=True))
    with pytest.raises(ToolError) as ei:
        await _jobbed(deps, "analyze_large", ProgressEmitter(FakeCtx()),
                      {"task": "t", "body": "small"}, 25,
                      lambda em, pr: analyze_large_tool(deps, em, pr))
    assert "quota_exhausted" in str(ei.value)
