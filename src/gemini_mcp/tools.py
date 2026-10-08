"""Tool implementations, decoupled from the MCP SDK (testable with a fake ctx).
Structured quota errors are raised as ToolError carrying the FR-B8 JSON payload.
Long jobs run as tracked background tasks (jobs.py): the inline call waits up to
wait_seconds, then returns a running-ticket instead of dying with the client.
Every compute response starts with a one-line 'report' (route · thinking · model ·
spend · rough edges) — clients truncate tool DESCRIPTIONS, so the reporting rule
travels inside the payload itself (postmortem-driven, v0.5.0)."""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gemini_router import QuotaExhaustedError, Router, RouterRequest, iso, pt_midnight_ts
from mcp.server.mcpserver.exceptions import ToolError

from . import ingest
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
    router_optimal: Router | None = None    # availability-ordered chain (route=optimal)


_ROUGH_KEYS = ("retry", "retrying", "wait", "503", "fallback", "failed",
               "cooldown", "overload")


def _report(meta: dict) -> str:
    """One-line report the agent MUST relay to chat — travels in the payload because
    clients truncate tool descriptions (postmortem p01 follow-up)."""
    rough = [e for e in meta.get("progress_trace") or []
             if any(k in (e.get("message") or "").lower() for k in _ROUGH_KEYS)]
    parts = [f"route={meta.get('route', '?')}",
             f"thinking={meta.get('thinking_level', '?')}",
             f"model={meta.get('model_final') or meta.get('model') or '?'}",
             f"req={meta.get('requests_spent', 1)}"
             + (f" (cached {meta['cache_hits']})" if meta.get("cache_hits") else ""),
             f"{meta.get('elapsed_s', meta.get('latency_s', '?'))}s"]
    failed = (meta.get("chunks") or {}).get("failed") or []
    if failed:
        parts.append(f"failed_chunks={failed}")
    if rough:
        parts.append(f"rough_edges={len(rough)} (retries/waits/fallbacks — details in "
                     f"meta.progress_trace)")
    else:
        parts.append("clean run")
    return " · ".join(str(p) for p in parts)


def _apply_route(deps: Deps, route: str | None, overflow: str | None) -> tuple[Router, str, str]:
    """Resolve (router, overflow, route). 'optimal' (default): empirical-availability
    chain, overflow auto-upgraded to 'lite+gemma'. 'best_available': newest-first
    config chain, overflow strictly opt-in (legacy hard-stop)."""
    r = route or deps.app_cfg.routing.default_route
    if r == "optimal":
        router = deps.router_optimal or deps.router
        ovf = overflow if overflow not in (None, "off") else "lite+gemma"
    else:
        router, ovf = deps.router, overflow or "off"
    return router, ovf, r


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
    router, p.overflow, p.route = _apply_route(deps, p.route, p.overflow)
    pipeline = Pipeline(router, deps.app_cfg, emitter, deps.logger)
    try:
        text, meta = await pipeline.analyze(p)
    except (QuotaExhaustedError, PlanBudgetError) as e:
        raise _fail(deps, emitter, "analyze_large", p.request_id, e) from e
    meta = {**meta, "route": p.route, "overflow": p.overflow,
            "model_final": (meta.get("models_used") or [None])[-1]}
    deps.audit.record({
        "ts": time.time(), "tool": "analyze_large", "request_id": p.request_id,
        "route": p.route, "overflow": p.overflow,
        "task_chars": len(p.task), "body_chars": len(p.body or ""),
        "thinking_level": meta["thinking_level"], "models_used": meta["models_used"],
        "chunks": meta["chunks"].get("count", 0), "failed_chunks": meta["chunks"].get("failed", []),
        "requests_spent": meta["requests_spent"], "cache_hits": meta["cache_hits"],
        "elapsed_s": meta["elapsed_s"], "result_chars": len(text),
        "body": (p.body or "") if deps.app_cfg.logging.dump_bodies else None,
        "result": text if deps.app_cfg.logging.dump_bodies else None,
    })
    return {"report": _report(meta), "result": text, "meta": meta}


async def gemini_generate_tool(deps: Deps, emitter: ProgressEmitter, params: dict) -> dict:
    router, ovf, route = _apply_route(deps, params.get("route"), params.get("overflow"))
    await emitter.stage("received", 3,
                        f"single-shot: {len(params['prompt'])} chars, route={route}")
    req = RouterRequest(
        prompt=params["prompt"], system=params.get("system"),
        thinking_level=params.get("thinking_level") or deps.app_cfg.thinking.default,
        output_format=params.get("output_format", "text"),
        max_output_tokens=params.get("max_output_tokens"), model=params.get("model"),
        overflow=ovf, wait=params.get("wait_for_minute_limits"),
        request_id=params.get("request_id"), source="mcp", kind="single")
    try:
        res = await router.complete(req, on_event=emitter.router_event_sync)
    except QuotaExhaustedError as e:
        raise _fail(deps, emitter, "gemini_generate", req.request_id, e) from e
    await emitter.stage("done", 100, f"done on {res.model} in {res.latency_s:.1f}s")
    await emitter.drain()
    meta = {"request_id": res.request_id, "model": res.model, "key_id": res.key_id,
            "tokens_in": res.tokens_in, "tokens_out": res.tokens_out,
            "latency_s": res.latency_s, "cached": res.cached,
            "overflow_tier": res.overflow_tier, "quota_after": res.quota_after,
            "route": route, "overflow": ovf, "thinking_level": req.thinking_level,
            "progress_trace": emitter.trace}
    deps.audit.record({"ts": time.time(), "tool": "gemini_generate",
                       "request_id": res.request_id, "model": res.model,
                       "route": route, "overflow": ovf,
                       "prompt_chars": len(req.prompt), "cached": res.cached,
                       "tokens_in": res.tokens_in, "tokens_out": res.tokens_out,
                       "elapsed_s": res.latency_s})
    return {"report": _report(meta), "result": res.text, "meta": meta}


async def file_upload_ticket_tool(deps: Deps, filename: str, size_bytes: int) -> dict:
    cfg = deps.app_cfg
    max_bytes = cfg.ingest.max_file_mb * 1_048_576
    if size_bytes <= 0:
        raise ToolError(json.dumps({"error": "bad_size",
                                    "hint": "size_bytes must be > 0"}))
    if size_bytes > max_bytes:
        raise ToolError(json.dumps({"error": "file_too_large", "bytes": size_bytes,
                                    "max_bytes": max_bytes}))
    token = os.environ.get("GEMINI_MCP_TOKEN", "")
    inbox = Path(cfg.ingest.dir)
    ingest.prune(inbox, cfg.ingest.keep_hours, cfg.ingest.total_cap_mb)
    upload_id, exp = ingest.issue_ticket(token, cfg.ingest.ticket_ttl_s)
    ingest.reserve(inbox, upload_id, filename, size_bytes)
    sig = ingest.make_ticket(token, upload_id, exp, size_bytes)
    url = (f"http://{cfg.server.http_bind}:{cfg.server.http_port}"
           f"/v1/upload/{upload_id}?exp={exp}&size={size_bytes}&sig={sig}")
    out: dict = {
        "upload_id": upload_id, "upload_url": url,
        "expires_in_s": cfg.ingest.ticket_ttl_s,
        "instructions": (f"ONE command: curl -T '{filename}' '{url}'  (through the "
                         f":{cfg.server.http_port} tunnel; the signature IS the auth, "
                         f"no headers needed). Then call analyze_file(task=…, "
                         f"upload_id='{upload_id}').")}
    if cfg.ingest.scp_target:
        out["scp_path"] = f"{cfg.ingest.scp_target}:{(inbox / upload_id).resolve()}"
        out["instructions_scp"] = (f"alternative without the tunnel: "
                                   f"scp '{filename}' '{out['scp_path']}'")
    deps.audit.record({"ts": time.time(), "tool": "file_upload_ticket",
                       "upload_id": upload_id, "filename": filename,
                       "size_bytes": size_bytes})
    return out


async def analyze_file_tool(deps: Deps, emitter: ProgressEmitter, params: dict) -> dict:
    """Server-side file analysis (D11): load inbox blob → sniff (binary → fail-fast,
    ZERO spend) → extract text (RTF server-side) → full analyze_large pipeline."""
    cfgi = deps.app_cfg.ingest
    inbox = Path(cfgi.dir)
    upload_id = params["upload_id"]
    try:
        side = ingest.load_sidecar(inbox, upload_id)
        path = inbox / upload_id
        if not path.exists():
            raise ingest.IngestError("upload_not_found", upload_id=upload_id,
                                     hint="ticket reserved but bytes never arrived — "
                                          "run the curl/scp upload from the ticket")
        data = path.read_bytes()
        reason = ingest.sniff_binary(data, side.get("filename", ""), cfgi.use_pandoc)
        if reason:
            raise ingest.IngestError(
                "binary_file_needs_conversion", reason=reason,
                filename=side.get("filename"), bytes=len(data),
                hint="server-side converters (docx/pdf/zip) are deferred; NOTHING was "
                     "sent to any model (zero quota spent). Upload .txt/.md/.csv/.rtf "
                     "(RTF is extracted server-side) or convert first.")
        text, enc, extractor = ingest.extract_text(
            data, side.get("filename", ""), cfgi.use_pandoc, cfgi.pandoc_timeout_s)
    except ingest.IngestError as e:
        deps.audit.record({"ts": time.time(), "tool": "analyze_file",
                           "upload_id": upload_id, "error": e.payload["error"]})
        raise ToolError(json.dumps(e.payload, ensure_ascii=False)) from e
    await emitter.stage("file_loaded", 1,
                        f"{side.get('filename')}: {len(data)} bytes → {len(text)} "
                        f"chars ({extractor}/{enc})")
    sub = {k: v for k, v in params.items() if k != "upload_id"}
    sub["body"] = text
    out = await analyze_large_tool(deps, emitter, sub)
    out["meta"]["file"] = {"filename": side.get("filename"), "bytes": len(data),
                           "chars": len(text), "encoding": enc, "extractor": extractor}
    out["report"] = (f"file={side.get('filename')} ({len(text)} chars via "
                     f"{extractor}) · {out['report']}")
    deps.audit.record({"ts": time.time(), "tool": "analyze_file",
                       "upload_id": upload_id, "filename": side.get("filename"),
                       "bytes": len(data), "chars": len(text), "extractor": extractor})
    return out


async def quota_status_tool(deps: Deps) -> dict:
    router = _apply_route(deps, None, None)[0]     # default-route view
    rows = router.quota_table()
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
