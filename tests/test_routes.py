"""Route profiles (v0.4.0): 'optimal' (default) selects the availability-ordered
router and auto-upgrades overflow to lite+gemma; 'best_available' keeps the legacy
newest-first chain with opt-in overflow. No network (b01)."""
from __future__ import annotations

from conftest import FakeCtx, FakeRouter, make_deps
from gemini_mcp.progress import ProgressEmitter
from gemini_mcp.tools import analyze_large_tool, gemini_generate_tool


def _deps_two():
    deps = make_deps(router=FakeRouter())
    deps.router_optimal = FakeRouter()
    return deps


async def test_optimal_route_is_default():
    deps = _deps_two()
    out = await gemini_generate_tool(deps, ProgressEmitter(FakeCtx()), {"prompt": "hi"})
    assert deps.router_optimal.calls and not deps.router.calls
    req = deps.router_optimal.calls[0]
    assert req.overflow == "lite+gemma"          # auto-upgraded on optimal
    assert req.thinking_level == "medium"        # new app default
    assert out["meta"]["route"] == "optimal" and out["meta"]["overflow"] == "lite+gemma"
    assert out["meta"]["thinking_level"] == "medium"


async def test_best_available_route_keeps_legacy():
    deps = _deps_two()
    out = await gemini_generate_tool(deps, ProgressEmitter(FakeCtx()),
                                     {"prompt": "hi", "route": "best_available"})
    assert deps.router.calls and not deps.router_optimal.calls
    assert deps.router.calls[0].overflow == "off"
    assert out["meta"]["route"] == "best_available"


async def test_optimal_route_respects_explicit_overflow():
    deps = _deps_two()
    await gemini_generate_tool(deps, ProgressEmitter(FakeCtx()),
                               {"prompt": "hi", "overflow": "lite"})
    assert deps.router_optimal.calls[0].overflow == "lite"


async def test_analyze_large_route_meta_and_audit():
    deps = _deps_two()
    out = await analyze_large_tool(deps, ProgressEmitter(FakeCtx()),
                                   {"task": "t", "body": "small"})
    assert out["meta"]["route"] == "optimal"
    assert out["meta"]["model_final"] == "gemini-3.8-flash"     # FakeRouter's model
    assert deps.router_optimal.calls and not deps.router.calls
    assert any(e.get("route") == "optimal" for e in deps.audit.entries)


async def test_optimal_router_absent_falls_back_to_main():
    deps = make_deps(router=FakeRouter())     # router_optimal is None
    await gemini_generate_tool(deps, ProgressEmitter(FakeCtx()), {"prompt": "hi"})
    assert deps.router.calls and deps.router.calls[0].overflow == "lite+gemma"
