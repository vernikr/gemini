"""Pipeline contracts (FR-D1..D8, G1-G4): single vs chunked path, chunk markers,
zero-spend fail-fast, recursive consolidation, partial failures, progress, meta."""
from __future__ import annotations

import pytest
from gemini_router import RouterError

from conftest import FakeCtx, FakeRouter, big_body, make_app_cfg
from gemini_mcp.pipeline import AnalyzeParams, Pipeline, PlanBudgetError
from gemini_mcp.progress import ProgressEmitter


async def _run(router, cfg, params, ctx=None):
    em = ProgressEmitter(ctx or FakeCtx())
    text, meta = await Pipeline(router, cfg, em).analyze(params)
    return text, meta


async def test_single_pass_short_body():
    router = FakeRouter()
    text, meta = await _run(router, make_app_cfg(), AnalyzeParams(task="summarize",
                                                                  body="tiny body"))
    assert text.startswith("SINGLE[")
    assert meta["chunks"]["count"] == 0 and meta["requests_spent"] == 1
    assert [c.kind for c in router.calls] == ["single"]
    assert router.calls[0].system == "summarize"          # task as system instruction
    stages = [t["stage"] for t in meta["progress_trace"]]
    assert stages[0] == "received" and "measured" in stages and stages[-1] == "done"


async def test_empty_body_skips_measurement():
    router = FakeRouter()
    text, meta = await _run(router, make_app_cfg(), AnalyzeParams(task="just a task",
                                                                  body=""))
    assert text.startswith("SINGLE[") and not router.count_calls
    assert meta["chunks"]["count"] == 0


async def test_chunked_pass_markers_and_consolidation():
    router = FakeRouter()
    # chunk budget large enough that the stitched answers fit one consolidation call;
    # the recursive case is covered by test_recursive_consolidation_*
    cfg = make_app_cfg(chunk_threshold_tokens=100, chunk_size_tokens=400)
    ctx = FakeCtx()
    body = big_body(200)
    text, meta = await _run(router, cfg,
                            AnalyzeParams(task="t", body=body, request_id="rid"), ctx)
    m = meta["chunks"]["count"]
    kinds = [c.kind for c in router.calls]
    assert m >= 2 and kinds.count("chunk") == m and kinds.count("consolidate") == 1
    for i, c in enumerate([c for c in router.calls if c.kind == "chunk"], 1):
        assert f"[SYSTEM NOTE — CHUNK {i}/{m}]" in c.prompt     # FR-D3 marker
        assert c.request_id == f"rid:c{i}"                       # per-chunk idempotency
        assert "Do NOT summarize the whole document" in c.prompt
    cons = router.calls[-1]
    assert cons.request_id == "rid:cons1"
    assert all(f"analysis-of-chunk-{i}-of-{m}" in cons.prompt for i in range(1, m + 1))
    assert text.startswith("CONSOLIDATED[")
    assert meta["requests_spent"] == m + 1 and meta["measured_tokens"] > 100
    assert meta["chunks"]["consolidation_rounds"] == 1
    stages = [t["stage"] for t in meta["progress_trace"]]
    for expected in ("plan", "budget_checked", "chunk_done", "consolidating", "done"):
        assert expected in stages
    assert ctx.progress and ctx.progress[-1][0] == 100.0        # FR-E1 countdown to 100
    pcts = [t["pct"] for t in meta["progress_trace"]]
    assert pcts == sorted(pcts)                                  # monotonic progress


async def test_recursive_consolidation_when_stitched_too_big():
    router = FakeRouter()
    cfg = make_app_cfg(chunk_threshold_tokens=100, chunk_size_tokens=60,
                       consolidation_fanin=2)
    text, meta = await _run(router, cfg, AnalyzeParams(task="t", body=big_body(200)))
    cons = [c for c in router.calls if c.kind == "consolidate"]
    assert meta["chunks"]["consolidation_rounds"] >= 2
    assert len(cons) >= 3                                        # group merges + final
    assert text.startswith("CONSOLIDATED[")


async def test_budget_fail_fast_zero_spend():
    budgets = [{"model": "gemini-3.8-flash", "rpd_left": 1, "rpm_wait_s": 0,
                "tpm_wait_s": 0, "cooldown_until": None, "best_wait_s": 0}]
    router = FakeRouter(budgets=budgets)
    cfg = make_app_cfg(chunk_threshold_tokens=100, chunk_size_tokens=60)
    with pytest.raises(PlanBudgetError) as ei:
        await _run(router, cfg, AnalyzeParams(task="t", body=big_body(200)))
    assert router.calls == []                                    # ZERO spend (FR-D4, G3)
    d = ei.value.to_dict()
    assert d["error"] == "plan_budget_insufficient"
    assert d["requests_needed"] > d["requests_available"] and "overflow" in d["hint"]


async def test_overflow_opt_in_extends_available_budget():
    budgets = [{"model": "gemini-3.8-flash", "rpd_left": 1, "rpm_wait_s": 0,
                "tpm_wait_s": 0, "cooldown_until": None, "best_wait_s": 0}]
    router = FakeRouter(budgets=budgets)
    cfg = make_app_cfg(chunk_threshold_tokens=100, chunk_size_tokens=60)
    with pytest.raises(PlanBudgetError):
        await _run(router, cfg, AnalyzeParams(task="t", body=big_body(60)))
    text, meta = await _run(router, cfg, AnalyzeParams(
        task="t", body=big_body(60), overflow="lite+gemma"))     # +500 gemma headroom
    assert meta["chunks"]["count"] >= 1 and text.startswith("CONSOLIDATED[")


async def test_chunk_failure_continue_with_gap():
    router = FakeRouter(fail_chunks={2})
    cfg = make_app_cfg(chunk_threshold_tokens=100, chunk_size_tokens=200,
                       fail_on_chunk_error=False)
    text, meta = await _run(router, cfg, AnalyzeParams(task="t", body=big_body(200)))
    assert meta["chunks"]["failed"] == [2]
    assert "Parts [2]" in router.calls[-1].prompt                # consolidation knows
    assert text.startswith("CONSOLIDATED[")
    stages = [t["stage"] for t in meta["progress_trace"]]
    assert "chunk_failed" in stages


async def test_chunk_failure_aborts_by_default():
    router = FakeRouter(fail_chunks={2})
    cfg = make_app_cfg(chunk_threshold_tokens=100, chunk_size_tokens=200)
    with pytest.raises(RouterError, match="simulated chunk failure"):
        await _run(router, cfg, AnalyzeParams(task="t", body=big_body(200)))
    chunk_calls = [c for c in router.calls if c.kind == "chunk"]
    assert len(chunk_calls) == 2                                 # stopped at failure


async def test_result_truncation():
    router = FakeRouter()
    text, meta = await _run(router, make_app_cfg(),
                            AnalyzeParams(task="t", body="tiny", result_max_chars=6))
    assert len(text) == 6 and meta["truncated"] is True


async def test_thinking_default_and_override():
    router = FakeRouter()
    cfg = make_app_cfg()
    await _run(router, cfg, AnalyzeParams(task="t", body="tiny"))
    assert router.calls[0].thinking_level == "high"              # app default
    await _run(router, cfg, AnalyzeParams(task="t", body="tiny2", thinking_level="low"))
    assert router.calls[1].thinking_level == "low"               # per-request override


async def test_empty_answer_aborts_by_default():
    """Live-smoke regression (router v0.1.1): thinking-starved empty text fails loudly."""
    router = FakeRouter()
    router.empty_chunks = {2}
    cfg = make_app_cfg(chunk_threshold_tokens=100, chunk_size_tokens=200)
    with pytest.raises(RouterError, match="empty answer"):
        await _run(router, cfg, AnalyzeParams(task="t", body=big_body(200)), FakeCtx())


async def test_empty_answer_continue_with_gap():
    router = FakeRouter()
    router.empty_chunks = {1}
    cfg = make_app_cfg(chunk_threshold_tokens=100, chunk_size_tokens=200,
                       fail_on_chunk_error=False)
    text, meta = await _run(router, cfg, AnalyzeParams(task="t", body=big_body(200)),
                            FakeCtx())
    assert meta["chunks"]["failed"] == [1]
    assert "Parts [1]" in router.calls[-1].prompt                # gap noted to the model
