"""Async ticket flow (jobs.py + _jobbed + analyze_result): inline fast path,
running-ticket on slow jobs, fetch-until-done, dedupe by request_id, error
propagation, unknown-id hint. No network (b01)."""
from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import FakeCtx, FakeRouter, make_deps
from gemini_mcp.jobs import JobManager
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

# ---- FR-G1: live job visibility on the polling surface --------------------------

async def test_running_ticket_carries_live_status():
    deps = make_deps(router=SlowRouter(0.4))
    srv = build_server(deps)
    res = await srv.call_tool("analyze_large",
                              {"task": "t", "body": "small", "wait_seconds": 0})
    ticket = _payload(res)
    assert ticket["status"] == "running"
    live = ticket["live"]                      # phase exists from the moment of submit
    assert live["phase"] and 0.0 <= live["pct"] <= 100.0
    res2 = await srv.call_tool("analyze_result",
                               {"request_id": ticket["request_id"], "wait_seconds": 3})
    assert _payload(res2)["result"].startswith("SINGLE[")


def test_live_phase_map_from_router_events():
    from gemini_router import RouterEvent
    live: dict = {}
    em = ProgressEmitter(None, live=live)
    em.router_event_sync(RouterEvent(stage="waiting", model="gemini-3.8-flash",
                                     key_id="abcd1234", wait_s=9.0,
                                     detail={"reason": "rpm"}))
    pub = JobManager._live_public({"live": live})
    assert pub["phase"] == "waiting_rpm"
    assert pub["model"] == "gemini-3.8-flash" and pub["key_id"] == "abcd1234"
    assert 8.0 < pub["eta_s"] <= 9.0
    assert "waiting" in pub["stage_line"] and "rpm" in pub["stage_line"]
    em.router_event_sync(RouterEvent(stage="dispatch", model="gemini-3.8-flash",
                                     key_id="abcd1234",
                                     detail={"row_id": 1, "est_tokens": 5}))
    pub = JobManager._live_public({"live": live})
    assert pub["phase"] == "generating" and "eta_s" not in pub


async def test_gateway_stage_updates_and_clears_eta():
    from gemini_router import RouterEvent
    live: dict = {}
    em = ProgressEmitter(None, live=live)
    em.router_event_sync(RouterEvent(stage="pacing", model="m", key_id="k", wait_s=2.0))
    assert JobManager._live_public({"live": live})["phase"] == "pacing"
    await em.stage("chunking", 40, "chunking 3 parts")
    pub = JobManager._live_public({"live": live})
    assert pub["phase"] == "chunking" and pub["pct"] == 40 and "eta_s" not in pub


async def test_live_updates_visible_on_polling_ticket():
    deps = make_deps()

    async def runner(em, params):
        await em.stage("chunking", 30, "chunking 3 parts")
        await asyncio.sleep(0.4)
        return {"result": "ok", "meta": {}}

    ticket = await _jobbed(deps, "analyze_large", ProgressEmitter(None),
                           {"task": "t", "body": "x", "request_id": "live-1"},
                           0, runner)
    assert ticket["status"] == "running"
    out = await analyze_result_tool(deps, "live-1", 0)
    assert out["status"] == "running"
    assert out["live"]["phase"] == "chunking"
    assert out["live"]["stage_line"] == "chunking 3 parts"
    assert out["live"]["pct"] == 30
    done = await analyze_result_tool(deps, "live-1", 2)
    assert done["result"] == "ok" and "live" not in done
