"""Agent-facing reporting: one wait episode, monotone group progress, every model."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from gemini_router import RouterEvent
from mcp.server.mcpserver.exceptions import ToolError

from conftest import FakeCtx, FakeRouter, make_app_cfg, make_deps
from gemini_mcp import ingest
from gemini_mcp.config import IngestCfg
from gemini_mcp.progress import ProgressEmitter
from gemini_mcp.server import INSTRUCTIONS, build_server
from gemini_mcp.tools import _report, _rough_episodes, file_upload_ticket_tool


def _event(stage, model="flash", reason=None, wait_s=None):
    return RouterEvent(stage=stage, model=model, key_id="key1",
                       wait_s=wait_s, detail={"reason": reason} if reason else {})


def test_repeated_gate_ticks_form_one_episode_and_trace_remains_detailed():
    em = ProgressEmitter()
    for sec in (60, 58, 56, 54):
        em.router_event_sync(_event("waiting", reason="tpm", wait_s=sec))
    report = _report({"model": "gemini-3.8-flash", "progress_trace": em.trace})
    assert "rough_edges=1" in report and "⚠️" in report
    assert len(em.trace) == 4 and len(_rough_episodes(em.trace)) == 1
    em.router_event_sync(_event("dispatch"))
    em.router_event_sync(_event("waiting", reason="tpm", wait_s=60))
    em.router_event_sync(_event("waiting", reason="rpm", wait_s=8))
    assert len(_rough_episodes(em.trace)) == 3  # a new wait; a different gate
    em.router_event_sync(_event("retry", reason="http_503"))
    assert len(_rough_episodes(em.trace)) == 4
    # Metadata inside a router:routing event must not be read as an actual wait.
    em.router_event_sync(_event("routing"))
    assert len(_rough_episodes(em.trace)) == 4


def test_span_and_high_water_do_not_mark_nested_file_finished_early():
    import asyncio

    async def run():
        live = {}
        em = ProgressEmitter(None, live=live)
        await em.stage("loading", 10)
        with em.progress_span(15, 25):
            await em.stage("received", 2)
            await em.stage("done", 100, "first file done")
            assert live["phase"] == "file_complete"
            assert live["pct"] == 25
        await em.stage("measured", 20)  # stale estimate; high-water stays 25
        with em.progress_span(35, 75):
            await em.stage("splitting", 10)
            await em.stage("done", 100, "PDF file done")
            assert live["pct"] == 75 and live["phase"] == "file_complete"
        await em.stage("group", 90)
        await em.stage("consolidating", 92)
        await em.stage("done", 100, "whole job done")
        assert live["phase"] == "finishing"
        assert [x["pct"] for x in em.trace] == sorted(x["pct"] for x in em.trace)
        assert all(x["pct"] < 100 for x in em.trace[:-1])
    asyncio.run(run())


class TwoWeakThenFlash(FakeRouter):
    async def complete(self, req, on_event=None, files=None):
        res = await super().complete(req, on_event=on_event, files=files)
        if req.kind == "single":
            return replace(res, model=("gemini-3.5-flash-lite" if
                                       len(self.calls) == 1 else "gemma-4-31b-it"))
        return replace(res, model="gemini-3.8-flash")


async def _uploaded(deps, filename, data):
    ticket = await file_upload_ticket_tool(deps, filename, len(data))
    ingest.save_upload(Path(deps.app_cfg.ingest.dir), ticket["upload_id"],
                       data, 50 * 2**20)
    return ticket["upload_id"]


async def test_mcp_two_weak_models_badged_even_with_strong_final(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_MCP_TOKEN", "tok123")
    app_cfg = make_app_cfg(tmp_path / "logs", chunk_size_tokens=100000).model_copy(
        update={"ingest": IngestCfg(dir=str(tmp_path / "inbox"), use_pandoc=False)})
    deps = make_deps(router=TwoWeakThenFlash(), app_cfg=app_cfg)
    ids = [await _uploaded(deps, f"file{i}.txt", ("hello world " * 40).encode())
           for i in (1, 2)]
    # Actual MCP registration/serialization, not just a private helper test.
    result = await build_server(deps).call_tool(
        "analyze_files", {"task": "Summarize", "upload_ids": ids, "wait_seconds": None})
    assert not getattr(result, "is_error", False)
    payload = getattr(result, "structured_content", None) or json.loads(result.content[0].text)
    report = payload["report"]
    assert report.startswith("files=2") and "⚠️" in report
    assert "⚠️ gemini-3.5-flash-lite" in report
    assert "⚠️ gemma-4-31b-it" in report
    assert "gemini-3.8-flash" in report
    assert payload["meta"]["models_used"][-1] == "gemini-3.8-flash"
    trace = payload["meta"]["progress_trace"]
    assert [e["pct"] for e in trace] == sorted(e["pct"] for e in trace)
    assert all(e["pct"] < 100 for e in trace[:-1])


async def test_mcp_failure_carries_blocker_badge_inline_and_async():
    deps = make_deps(router=FakeRouter(raise_quota=True))
    srv = build_server(deps)
    with pytest.raises(ToolError) as inline:
        await srv.call_tool("gemini_generate", {"prompt": "hello",
                                                "wait_seconds": None})
    assert "⛔️" in str(inline.value)
    try:
        running = await srv.call_tool("gemini_generate", {"prompt": "hello",
                                                           "wait_seconds": 0})
    except ToolError as e:
        assert "⛔️" in str(e)  # the job finished during submission
    else:
        p = getattr(running, "structured_content", None) or \
            json.loads(running.content[0].text)
        assert p["status"] == "running"
        with pytest.raises(ToolError) as outcome:
            await srv.call_tool("analyze_result", {"request_id": p["request_id"],
                                                   "wait_seconds": 2})
        assert "⛔️" in str(outcome.value)
    assert "⚠️" in INSTRUCTIONS and "⛔️" in INSTRUCTIONS


async def test_upload_ticket_failure_carries_badge_without_job_wrapper():
    with pytest.raises(ToolError) as outcome:
        await build_server(make_deps()).call_tool(
            "file_upload_ticket", {"filename": "x.pdf", "size_bytes": 0})
    assert "⛔️" in str(outcome.value) and "bad_size" in str(outcome.value)


async def test_temporary_gate_not_counted_as_many_failures():
    # Group trace exercises caller-facing _report, not just _rough_episodes.
    em = ProgressEmitter(FakeCtx())
    for wait in (8, 6, 4, 2):
        em.router_event_sync(_event("waiting", reason="rpm", wait_s=wait))
    assert "rough_edges=1" in _report({"progress_trace": em.trace})
