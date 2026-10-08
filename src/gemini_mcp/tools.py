"""Tool implementations, decoupled from the MCP SDK (testable with a fake ctx).
Structured quota errors are raised as ToolError carrying the FR-B8 JSON payload.
Long jobs run as tracked background tasks (jobs.py): the inline call waits up to
wait_seconds, then returns a running-ticket instead of dying with the client."""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from gemini_router import QuotaExhaustedError, Router, RouterRequest, iso, pt_midnight_ts
from mcp.server.mcpserver.exceptions import ToolError

from .config import AppCfg
from .jobs import JobManager
from .logging_setup import AuditLogger
from .pipeline import AnalyzeParams, Pipeline, PlanBudgetError
from .progress import ProgressEmitter


@dataclass(slots=True)
class Deps:
    router: Router
    app_cfg: AppCfg
    audit: AuditLogger
    logger: logging.Logger
    jobs: JobManager = field(default_factory=JobManager)


async def _jobbed(deps: Deps, tool: str, emitter: ProgressEmitter, params: dict,
                  wait_seconds: int | None,
                  runner: Callable[[ProgressEmitter, dict], Any]) -> dict:
    """wait_seconds None → classic inline run. Otherwise: background job keyed by
    request_id (generated if absent); inline wait ≤ wait_seconds, then a ticket."""
    if wait_seconds is None:
        return await runner(emitter, params)
    key = params.get("request_id") or f"{tool}-{uuid.uuid4().hex[:12]}"
    params = {**params, "request_id": key}
    deps.audit.record({"ts": time.time(), "tool": tool, "request_id": key,
                       "event": "async_submitted", "wait_seconds": wait_seconds})
    status, payload = await deps.jobs.submit(
        key,
        lambda: runner(ProgressEmitter(None, deps.logger), params),   # detached ctx
        wait_seconds)
    if status == "done":
        return payload
    if status == "error":
        raise ToolError(payload.get("detail") or json.dumps(payload, ensure_ascii=False))
    deps.logger.info("%s %s: ticket issued after %.0fs inline wait",
                     tool, key, wait_seconds)
    return payload


async def analyze_result_tool(deps: Deps, request_id: str,
                              wait_seconds: int | None = 25) -> dict:
    status, payload = await deps.jobs.fetch(request_id, wait_seconds)
    if status == "done":
        return payload
    if status == "error":
        raise ToolError(payload.get("detail") or json.dumps(payload, ensure_ascii=False))
    return payload                     # running-ticket or unknown-hint, both dicts


def _fail(deps: Deps, emitter: ProgressEmitter, tool: str, request_id: str | None,
          e: QuotaExhaustedError | PlanBudgetError) -> ToolError:
    payload = e.to_dict()
    payload["progress_trace"] = emitter.trace
    deps.audit.record({"ts": time.time(), "tool": tool, "request_id": request_id,
                       "error": payload["error"], "payload": payload})
    deps.logger.warning("%s failed: %s", tool, payload["error"])
    return ToolError(json.dumps(payload, ensure_ascii=False, default=str))


async def analyze_large_tool(deps: Deps, emitter: ProgressEmitter, params: dict) -> dict:
    p = AnalyzeParams(**params)
    pipeline = Pipeline(deps.router, deps.app_cfg, emitter, deps.logger)
    try:
        text, meta = await pipeline.analyze(p)
    except (QuotaExhaustedError, PlanBudgetError) as e:
        raise _fail(deps, emitter, "analyze_large", p.request_id, e) from e
    deps.audit.record({
        "ts": time.time(), "tool": "analyze_large", "request_id": p.request_id,
        "task_chars": len(p.task), "body_chars": len(p.body or ""),
        "thinking_level": meta["thinking_level"], "models_used": meta["models_used"],
        "chunks": meta["chunks"].get("count", 0), "failed_chunks": meta["chunks"].get("failed", []),
        "requests_spent": meta["requests_spent"], "cache_hits": meta["cache_hits"],
        "elapsed_s": meta["elapsed_s"], "result_chars": len(text),
        "body": (p.body or "") if deps.app_cfg.logging.dump_bodies else None,
        "result": text if deps.app_cfg.logging.dump_bodies else None,
    })
    return {"result": text, "meta": meta}


async def gemini_generate_tool(deps: Deps, emitter: ProgressEmitter, params: dict) -> dict:
    await emitter.stage("received", 3, f"single-shot: {len(params['prompt'])} chars")
    req = RouterRequest(
        prompt=params["prompt"], system=params.get("system"),
        thinking_level=params.get("thinking_level") or deps.app_cfg.thinking.default,
        output_format=params.get("output_format", "text"),
        max_output_tokens=params.get("max_output_tokens"), model=params.get("model"),
        overflow=params.get("overflow", "off"), wait=params.get("wait_for_minute_limits"),
        request_id=params.get("request_id"), source="mcp", kind="single")
    try:
        res = await deps.router.complete(req, on_event=emitter.router_event_sync)
    except QuotaExhaustedError as e:
        raise _fail(deps, emitter, "gemini_generate", req.request_id, e) from e
    await emitter.stage("done", 100, f"done on {res.model} in {res.latency_s:.1f}s")
    await emitter.drain()
    meta = {"request_id": res.request_id, "model": res.model, "key_id": res.key_id,
            "tokens_in": res.tokens_in, "tokens_out": res.tokens_out,
            "latency_s": res.latency_s, "cached": res.cached,
            "overflow_tier": res.overflow_tier, "quota_after": res.quota_after,
            "progress_trace": emitter.trace}
    deps.audit.record({"ts": time.time(), "tool": "gemini_generate",
                       "request_id": res.request_id, "model": res.model,
                       "prompt_chars": len(req.prompt), "cached": res.cached,
                       "tokens_in": res.tokens_in, "tokens_out": res.tokens_out,
                       "elapsed_s": res.latency_s})
    return {"result": res.text, "meta": meta}


async def quota_status_tool(deps: Deps) -> dict:
    rows = deps.router.quota_table()
    rec = next((r for r in rows if r.get("recommended")), None)
    return {
        "rows": rows,
        "recommended_model": rec["model"] if rec else None,
        "recommended_key_id": rec["key_id"] if rec else None,
        "why": ("first model in chain order with daily budget left and no minute gate"
                if rec else
                "nothing available without waiting — see rpd_left/wait_s/cooldown_until "
                "and reset_eta_pt"),
        "reset_eta_pt": iso(pt_midnight_ts(deps.router.cfg.quota_day_tz, days_ahead=1)),
    }


async def router_logs_tool(deps: Deps, n: int = 20, model: str | None = None,
                           status: str | None = None, source: str | None = None,
                           request_id: str | None = None) -> dict:
    return {"calls": deps.router.ledger.calls_tail(
        n, model=model, status=status, source=source, request_id=request_id)}
