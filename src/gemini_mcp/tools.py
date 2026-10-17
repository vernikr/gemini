"""Tool implementations, decoupled from the MCP SDK (testable with a fake ctx).
Structured quota errors are raised as ToolError carrying the FR-B8 JSON payload.
Long jobs run as tracked background tasks (jobs.py): the inline call waits up to
wait_seconds, then returns a running-ticket instead of dying with the client.
Every compute response starts with a one-line 'report' (route · thinking · model ·
spend · rough edges) — clients truncate tool DESCRIPTIONS, so the reporting rule
travels inside the payload itself (postmortem-driven, v0.5.0)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gemini_router import (
    DEFAULT_LEVEL,
    QuotaExhaustedError,
    Router,
    RouterError,
    RouterRequest,
    UploadedFile,
    check_drift,
    forecast_media_tokens,
    iso,
    key_id,
    media_class,
    pt_midnight_ts,
)
from mcp.server.mcpserver.exceptions import ToolError

from . import ingest
from .config import AppCfg
from .jobs import JobManager
from .logging_setup import AuditLogger
from .pdf_split import PdfSplitError, split_pages
from .pipeline import AnalyzeParams, Pipeline, PlanBudgetError
from .progress import ProgressEmitter
from .prompts import group_prompt, split_prompt


@dataclass(slots=True)
class Deps:
    router: Router
    app_cfg: AppCfg
    audit: AuditLogger
    logger: logging.Logger
    jobs: JobManager = field(default_factory=JobManager)
    router_optimal: Router | None = None    # availability-ordered chain (route=optimal)
    backend: str = "router"                 # 'router' (fleet) | 'direct' (AI Studio)


# What counts as a rough edge, by STAGE first: text matching alone was fooled by the
# `routing` event, whose message embeds its detail dict — and that dict has a key called
# `wait`, so every live request reported an edge. Stages are the structured truth; the
# keyword scan only covers the non-router messages.
_ROUGH_STAGES = ("router:retry", "router:waiting", "router:switch", "router:pacing",
                 "router:overflow", "router:quota_exhausted")
_ROUGH_KEYS = ("retry", "503", "fallback", "failed", "cooldown", "overload", "timeout")


def _rough_episodes(trace: list[dict]) -> list[dict]:
    """Count events, not router poll ticks: one continuous minute wait is ONE wait.

    A non-wait event ends an episode, so a second wait after a dispatch/retry is
    counted independently. Keep the original trace intact for detailed diagnosis.
    """
    episodes: list[dict] = []
    prior_wait: tuple | None = None
    for event in trace:
        stage = event.get("stage", "")
        detail = event.get("detail") or {}
        if stage == "router:waiting":
            signature = (detail.get("model"), detail.get("key_id"),
                         detail.get("reason"))
            if signature != prior_wait:
                episodes.append(event)
            prior_wait = signature
        else:
            prior_wait = None
            if stage in _ROUGH_STAGES or (not stage.startswith("router:") and
                                           any(k in (event.get("message") or "").lower()
                                               for k in _ROUGH_KEYS)):
                episodes.append(event)
    return episodes


def _fallback_model(model: str | None) -> bool:
    return bool(model and ("flash-lite" in model.lower() or
                           "gemma" in model.lower()))


def _report(meta: dict) -> str:
    """One-line agent-facing receipt: flag every unrequested weak-tier model used.

    The host must be explicitly told which model(s) served the call, not just the
    final consolidation model (a strong final model can mask weaker partials).
    """
    rough = _rough_episodes(meta.get("progress_trace") or [])
    models = list(dict.fromkeys(m for m in
                                (meta.get("models_used") or [meta.get("model_final") or
                                                                meta.get("model")]) if m))
    requested = meta.get("requested_model")
    weak = [m for m in models if _fallback_model(m) and m != requested]
    def label(m: str) -> str:
        return f"⚠️ {m}" if m in weak else m
    model_part = (f"models=[{', '.join(label(m) for m in models)}]" if len(models) > 1
                  else f"model={label(models[0]) if models else '?'}")
    parts = [f"backend={meta.get('backend', '?')}",
             f"route={meta.get('route', '?')}",
             f"thinking={meta.get('thinking_level', '?')}", model_part,
             f"req={meta.get('requests_spent', 1)}"
             + (f" (cached {meta['cache_hits']})" if meta.get("cache_hits") else ""),
             f"{meta.get('elapsed_s', meta.get('latency_s', '?'))}s"]
    failed = (meta.get("chunks") or {}).get("failed") or []
    if failed:
        parts.append(f"failed_chunks={failed}")
    if rough:
        parts.append(f"rough_edges={len(rough)} (distinct waits/retries/switches, "
                     "NOT failed requests; details in meta.progress_trace)")
    if meta.get("degradations"):
        parts.append("; ".join(meta["degradations"]))
    if weak:
        parts.append("unrequested lite/gemma fallback — tell the user which "
                     "⚠️-marked models served the request")
    if not (rough or failed or weak or meta.get("degradations") or
            (meta.get("cost") or {}).get("drift")):
        parts.append("clean run")
    warning = bool(rough or failed or weak or meta.get("degradations") or
                   (meta.get("cost") or {}).get("drift"))
    return ("⚠️ " if warning else "") + " · ".join(str(p) for p in parts)


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


async def _marked_runner(runner, emitter: ProgressEmitter, params: dict) -> dict:
    """Keep the recovery cue inside structured errors, not only tool descriptions."""
    try:
        return await runner(emitter, params)
    except ToolError as e:
        try:
            payload = json.loads(str(e))
        except (json.JSONDecodeError, TypeError):
            payload = {"error": "tool_error", "detail": str(e)}
        if isinstance(payload, dict):
            payload.setdefault("agent_notice", "⛔️ Tell the user the call failed; "
                               "explain the hint and any quota already spent.")
            raise ToolError(json.dumps(payload, ensure_ascii=False)) from e
        raise


def _rid(tool: str, params: dict) -> str:
    """Every call carries an id, whether the caller supplied one or not.

    The owner's layering rule joins a consumer's thin "I asked / I got" record to this
    service's own log by request_id. Measured live on 2026-10-17, a call that did not
    supply one left BOTH sides with request_id = None — the audit row and the router
    ledger row — so the two logs could not be joined at all, and the chunk and
    consolidation sub-requests of one analyze job could not be grouped either, because
    Pipeline._rid derives theirs from this one. Generating it here fixes all of that in
    one place; the name matches the one _jobbed already used.
    """
    return params.get("request_id") or f"{tool}-{uuid.uuid4().hex[:12]}"


def _src(params: dict) -> str:
    """Who is calling. The CLI passes source="cli"; the tools used to overwrite it with
    a hardcoded "mcp", so the executor's own log could not tell the two surfaces apart —
    confirmed live: a CLI call landed in the ledger as source='mcp'."""
    return params.get("source") or "mcp"


async def _jobbed(deps: Deps, tool: str, emitter: ProgressEmitter, params: dict,
                  wait_seconds: int | None,
                  runner: Callable[[ProgressEmitter, dict], Any]) -> dict:
    """wait_seconds None → classic inline run. Otherwise: background job keyed by
    request_id (generated if absent); inline wait ≤ wait_seconds, then a ticket."""
    if wait_seconds is None:
        return await _marked_runner(runner, emitter, params)
    key = params.get("request_id") or f"{tool}-{uuid.uuid4().hex[:12]}"
    params = {**params, "request_id": key}
    deps.audit.record({"ts": time.time(), "tool": tool, "request_id": key,
                       "event": "async_submitted", "wait_seconds": wait_seconds})
    live: dict = {}                                        # FR-G1: job → poller mirror
    status, payload = await deps.jobs.submit(
        key,
        lambda: _marked_runner(runner, ProgressEmitter(None, deps.logger, live=live),
                               params),
        wait_seconds, live=live)
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


class _UploadFailed(Exception):
    """Structured payload for a provider upload that never produced a file."""

    def to_dict(self) -> dict:
        return {"error": "file_upload_failed",
                "hint": "the provider did not accept the file; NOTHING was analyzed and "
                        "no model quota was spent. Check the file, then retry — a fresh "
                        "attempt is allowed while the inbox blob is still there."}


_UPLOAD_FAILED = _UploadFailed()


def _fail(deps: Deps, emitter: ProgressEmitter, tool: str, request_id: str | None,
          e: QuotaExhaustedError | PlanBudgetError | _UploadFailed) -> ToolError:
    payload = e.to_dict()
    payload["progress_trace"] = emitter.trace
    deps.audit.record({"ts": time.time(), "tool": tool, "request_id": request_id,
                       "error": payload["error"], "payload": payload})
    deps.logger.warning("%s failed: %s", tool, payload["error"])
    return ToolError(json.dumps(payload, ensure_ascii=False, default=str))


_DOC_MARKERS = (("{\\rtf1", "RTF"), ("%PDF-", "PDF"), ("PK\x03\x04", "DOCX/XLSX (zip)"))

# The one statement of the file flow. Agents read it where they are, so it rides in two
# payloads — quota_status, which agents reliably call first, and the raw-document
# rejection — but both are this string, so the steps cannot drift apart.
FILE_FLOW = ("file_upload_ticket(filename, size_bytes) \u2192 HTTP PUT the bytes to the "
             "returned upload_url \u2192 analyze_file(upload_id, task). Several files "
             "with one answer: repeat ticket+upload per file, then "
             "analyze_files(upload_ids=[\u2026], task) — media is packed by measured "
             "tokens into group requests and consolidated. Text documents "
             "are extracted server-side (pandoc/RTF) and chunked; PDFs, images and "
             "audio go to the provider's Files API. Never inline "
             "raw file contents into a text tool: the markup is unreadable for the model "
             "(non-ASCII survives as hex escapes) and burns your context.")
# Container signatures only ever sit at offset 0; scanning the whole field would reject
# legitimate text that merely quotes one (a document ABOUT the PDF format, say).
_DOC_HEAD_CHARS = 4096


def _inline_doc_guard(text: str | None, field: str) -> None:
    """Fail fast when an agent inlines a raw document into a text tool: the markup is
    unreadable for the model (non-ASCII survives as hex escapes) and a context bomb for
    the client. Detect the leading format signature and answer with the file flow."""
    if not text:
        return
    head = text[:_DOC_HEAD_CHARS]
    for marker, kind in _DOC_MARKERS:
        if marker in head:
            raise ToolError(json.dumps({
                "error": "raw_document_inlined",
                "detected": kind,
                "field": field,
                "fix": (f"raw_document_inlined: {kind} markup detected in '{field}'. "
                        f"{FILE_FLOW} If you genuinely meant to analyze markup AS "
                        "text, escape or re-encode it first."),
            }, ensure_ascii=False))


def _record(deps: Deps, tool: str, meta: dict, **extra) -> None:
    """One audit row per operation, with HOW it went persisted — not only in the reply.

    The owner's layering rule: a consumer logs its own thin "I asked / I got" and
    nothing else; everything about how the request actually ran belongs here. That
    only holds if the internals survive the response. Until now they did not:
    `progress_trace` was computed, used to build the report line, returned once, and
    dropped — so the fallbacks, waits and model switches were unanswerable afterwards
    from any log. Retention is the existing one (audit_keep_days / audit_max_mb).
    """
    trace = meta.get("progress_trace") or []
    deps.audit.record({"ts": time.time(), "tool": tool, "request_id": meta.get("request_id"),
                       "rough_edges": len(_rough_episodes(trace)), "trace": trace,
                       **extra})


async def analyze_large_tool(deps: Deps, emitter: ProgressEmitter, params: dict) -> dict:
    params = {**params, "request_id": _rid("analyze_large", params)}
    p = AnalyzeParams(**params)
    _inline_doc_guard(p.body, "body")
    router, p.overflow, p.route = _apply_route(deps, p.route, p.overflow)
    pipeline = Pipeline(router, deps.app_cfg, emitter, deps.logger)
    try:
        text, meta = await pipeline.analyze(p)
    except (QuotaExhaustedError, PlanBudgetError) as e:
        raise _fail(deps, emitter, "analyze_large", p.request_id, e) from e
    meta = {**meta, "route": p.route, "overflow": p.overflow, "backend": deps.backend,
            "model_final": (meta.get("models_used") or [None])[-1],
            "requested_model": p.model}
    _record(deps, "analyze_large", meta,
            route=p.route, overflow=p.overflow,
            task_chars=len(p.task), body_chars=len(p.body or ""),
            thinking_level=meta["thinking_level"], models_used=meta["models_used"],
            chunks=meta["chunks"].get("count", 0),
            failed_chunks=meta["chunks"].get("failed", []),
            requests_spent=meta["requests_spent"], cache_hits=meta["cache_hits"],
            elapsed_s=meta["elapsed_s"], result_chars=len(text),
            body=(p.body or "") if deps.app_cfg.logging.dump_bodies else None,
            result=text if deps.app_cfg.logging.dump_bodies else None)
    return {"report": _report(meta), "result": text, "meta": meta}


async def gemini_generate_tool(deps: Deps, emitter: ProgressEmitter, params: dict) -> dict:
    params = {**params, "request_id": _rid("gemini_generate", params)}
    _inline_doc_guard(params.get("prompt"), "prompt")
    router, ovf, route = _apply_route(deps, params.get("route"), params.get("overflow"))
    await emitter.stage("received", 3,
                        f"single-shot: {len(params['prompt'])} chars, route={route}")
    req = RouterRequest(
        prompt=params["prompt"], system=params.get("system"),
        thinking_level=params.get("thinking_level") or deps.app_cfg.thinking.default,
        output_format=params.get("output_format", "text"),
        max_output_tokens=params.get("max_output_tokens"), model=params.get("model"),
        overflow=ovf, wait=params.get("wait_for_minute_limits"),
        request_id=params.get("request_id"), source=_src(params), kind="single")
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
            "route": route, "overflow": ovf, "backend": deps.backend,
            "thinking_level": req.thinking_level, "progress_trace": emitter.trace,
            "requested_model": req.model}
    _record(deps, "gemini_generate", meta,
            model=res.model, route=route, overflow=ovf,
            prompt_chars=len(req.prompt), cached=res.cached,
            tokens_in=res.tokens_in, tokens_out=res.tokens_out,
            elapsed_s=res.latency_s)
    return {"report": _report(meta), "result": res.text, "meta": meta}


async def file_upload_ticket_tool(deps: Deps, filename: str, size_bytes: int,
                                  override_size_limit: bool = False) -> dict:
    cfg = deps.app_cfg
    max_bytes = cfg.ingest.max_file_mb * 1_048_576
    if size_bytes <= 0:
        raise ToolError(json.dumps({"error": "bad_size",
                                    "hint": "size_bytes must be > 0",
                                    "agent_notice": "⛔️ Upload ticket failed; fix the size."},
                                   ensure_ascii=False))
    if size_bytes > max_bytes and not override_size_limit:
        raise ToolError(json.dumps({
            "error": "file_too_large", "bytes": size_bytes, "max_bytes": max_bytes,
            "agent_notice": "⛔️ Upload ticket failed; explain the size limit and fix.",
            "fix": "compress the file locally, split it across separate requests, or "
                   "ask the user for explicit permission to exceed the cap and retry "
                   "with override_size_limit=true"}, ensure_ascii=False))
    token = os.environ.get("GEMINI_MCP_TOKEN", "")
    inbox = Path(cfg.ingest.dir)
    await asyncio.to_thread(ingest.prune, inbox, cfg.ingest.keep_hours,
                            cfg.ingest.total_cap_mb)
    upload_id, exp = ingest.issue_ticket(token, cfg.ingest.ticket_ttl_s)
    if size_bytes > max_bytes:
        # The only gate here is the user's consent, so leave a record of what was
        # issued. Logged as well as returned: a chat transcript is not an audit trail.
        deps.logger.warning(
            "ingest: OVERSIZE ticket %s for %s — %d bytes against a %d-byte cap, "
            "issued on override_size_limit at the user's explicit request",
            upload_id, filename, size_bytes, max_bytes)
    ingest.reserve(inbox, upload_id, filename, size_bytes)
    sig = ingest.make_ticket(token, upload_id, exp, size_bytes)
    url = (f"http://{cfg.server.http_bind}:{cfg.server.http_port}"
           f"/v1/upload/{upload_id}?exp={exp}&size={size_bytes}&sig={sig}")
    out: dict = {
        "upload_id": upload_id, "upload_url": url,
        "expires_in_s": cfg.ingest.ticket_ttl_s,
        "oversize": size_bytes > max_bytes,
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


@dataclass(slots=True)
class _Loaded:
    """One inbox blob, resolved. `text is None` means the bytes are provider-bound: the
    blob stays on disk and is streamed to the Files API instead of being decoded."""

    side: dict
    nbytes: int
    text: str | None
    enc: str                 # encoding for text, MIME for provider-bound media
    extractor: str           # 'provider/<kind>' | 'pandoc/<fmt>→gfm' | 'rtf' | 'plain'
    path: Path
    pages: int | None = None     # pdf page count from the pre-flight; None if unknown


def _load_and_extract(inbox: Path, upload_id: str, cfgi) -> _Loaded:
    """Blocking half of analyze_file, run in a worker thread: pandoc is a subprocess and
    the RTF fallback is pure-Python parsing, either of which would freeze the whole
    daemon (healthz, keep-alives, every other client) for the duration.

    Returns the byte COUNT, not the bytes — the blob is freed as soon as text exists."""
    side = ingest.load_sidecar(inbox, upload_id)
    path = inbox / upload_id
    if not path.exists():
        raise ingest.IngestError("upload_not_found", upload_id=upload_id,
                                 hint="ticket reserved but bytes never arrived — "
                                      "run the curl/scp upload from the ticket")
    filename = side.get("filename", "")
    with path.open("rb") as fh:                 # the head decides; provider-bound blobs
        head = fh.read(65536)                   # are never read into memory at all
    media = ingest.media_kind(head, filename) if cfgi.files_api else None
    nbytes = path.stat().st_size
    if media:
        kind, mime = media
        pages = None
        if kind == "pdf":
            over = nbytes > cfgi.pdf_max_mb * 1_048_576
            if over:
                raise ingest.IngestError(
                    "pdf_too_large", bytes=nbytes,
                    max_bytes=cfgi.pdf_max_mb * 1_048_576,
                    hint="the provider refuses PDFs over this even though its staging "
                         "accepts far more (zero quota spent); split or compress it")
            pages = ingest.pdf_pages(path, cfgi.pandoc_timeout_s)
            if pages is not None and pages > cfgi.pdf_max_pages:
                raise ingest.IngestError(
                    "pdf_too_many_pages", pages=pages, max_pages=cfgi.pdf_max_pages,
                    hint="a single PDF is never split here; the provider refuses "
                         "documents over this page count (zero quota spent)")
        return _Loaded(side=side, nbytes=nbytes, text=None, enc=mime,
                       extractor=f"provider/{kind}", path=path, pages=pages)
    if not cfgi.files_api and ingest.media_kind(head, filename):
        kind, mime = ingest.media_kind(head, filename)
        raise ingest.IngestError(
            "files_api_disabled", kind=kind, mime=mime, filename=filename,
            hint="ingest.files_api is off, so this file cannot be read: only text "
                 "documents are supported. Enable it to send PDFs, images and audio "
                 "to the provider (zero quota spent)")
    data = path.read_bytes()
    reason = ingest.sniff_binary(data, filename, cfgi.use_pandoc)
    if reason:
        raise ingest.IngestError(
            "binary_file_needs_conversion", reason=reason,
            filename=side.get("filename"), bytes=len(data),
            hint="NOTHING was sent to any model (zero quota spent). PDF, images, audio "
                 "and video are read by the provider; text and office documents "
                 "(.txt .md .csv .rtf .docx .odt .epub .html) are extracted server-side. "
                 "Convert anything else first.")
    text, enc, extractor = ingest.extract_text(
        data, filename, cfgi.use_pandoc, cfgi.pandoc_timeout_s)
    return _Loaded(side=side, nbytes=len(data), text=text, enc=enc,
                   extractor=extractor, path=path)


async def analyze_file_tool(deps: Deps, emitter: ProgressEmitter, params: dict) -> dict:
    """Server-side file analysis (D11): load inbox blob → sniff (unsupported → fail-fast,
    ZERO spend) → text documents run the existing chunked pipeline, media goes
    through measured Files API requests (oversized PDFs may be split)."""
    params = {**params, "request_id": _rid("analyze_file", params)}
    cfgi = deps.app_cfg.ingest
    inbox = Path(cfgi.dir)
    upload_id = params["upload_id"]
    # Second prune trigger (the first is ticket issuance): a session that analyzes and
    # stops — never asking for another ticket — still ages its inbox out, spent-ticket
    # markers included. Runs before the blob is read, so a fresh upload is never the
    # oldest entry the cap evicts.
    await asyncio.to_thread(ingest.prune, inbox, cfgi.keep_hours, cfgi.total_cap_mb)
    try:
        loaded = await asyncio.to_thread(_load_and_extract, inbox, upload_id, cfgi)
    except ingest.IngestError as e:
        deps.audit.record({"ts": time.time(), "tool": "analyze_file",
                           "upload_id": upload_id, "error": e.payload["error"]})
        raise ToolError(json.dumps(e.payload, ensure_ascii=False)) from e
    filename = loaded.side.get("filename")
    if loaded.text is None:
        out = await _provider_file_analysis(deps, emitter, params, inbox, upload_id,
                                           loaded)
    else:
        await emitter.stage("file_loaded", 1,
                            f"{filename}: {loaded.nbytes} bytes → {len(loaded.text)} "
                            f"chars ({loaded.extractor}/{loaded.enc})")
        sub = {k: v for k, v in params.items() if k != "upload_id"}
        sub["body"] = loaded.text
        out = await analyze_large_tool(deps, emitter, sub)
        out["meta"]["file"] = {
            "filename": filename, "bytes": loaded.nbytes, "chars": len(loaded.text),
            "encoding": loaded.enc, "extractor": loaded.extractor, "via": "text"}
        out["report"] = (f"file={filename} ({len(loaded.text)} chars via "
                         f"{loaded.extractor}) · {out['report']}")
        deps.audit.record({"ts": time.time(), "tool": "analyze_file",
                           "upload_id": upload_id, "filename": filename,
                           "bytes": loaded.nbytes, "chars": len(loaded.text),
                           "extractor": loaded.extractor})
    return out


# A cached provider reference is reused while it has this long left: the provider drops
# files after 48 h, and a request sent into that edge would fail with a confusing 4xx.
_REF_MARGIN_S = 600


def _cached_upload(router: Router, loaded: _Loaded) -> UploadedFile | None:
    """The provider file this blob is already on, when it is still usable: unexpired and
    owned by a key the router still holds. The blob never changes for a given upload_id,
    so the reference cannot go stale while it exists."""
    ref = (loaded.side.get("provider") or {})
    if not ref.get("uri") or not ref.get("key_id"):
        return None
    exp = ingest.expires_ts(ref.get("expires_at"))
    if exp is not None and exp - time.time() <= _REF_MARGIN_S:
        return None
    if ref["key_id"] not in {key_id(k) for k in router.cfg.api_keys}:
        return None                        # the key was rotated out: it cannot see its file
    return UploadedFile(name=ref.get("name", ""), uri=ref["uri"],
                        mime=ref.get("mime") or loaded.enc, size=loaded.nbytes,
                        state="ACTIVE", expires_at=ref.get("expires_at"),
                        sha256=ref.get("sha256"), key_id=ref["key_id"])


def _drift_for(mime: str, pages: int | None, level: str | None,
               measured: int) -> tuple[dict | None, str]:
    """The wave-1 drift signal, shared by the single-file and group paths: compare the
    router's tariff forecast with the measured cost. A pdf without a page count has no
    honest forecast. Returns (drift-or-None, media class)."""
    cls = media_class(mime)
    if not measured or (cls == "pdf" and not pages):
        return None, cls
    try:
        predicted = forecast_media_tokens(mime, pages=pages or 1, level=level)
    except ValueError:
        return None, cls
    return check_drift(predicted, measured), cls


def _record_drift(deps: Deps, router: Router, key_id_: str, model: str, cls: str,
                  level: str | None, drift: dict, source: str = "mcp") -> None:
    led = getattr(router, "ledger", None)
    if led is not None and hasattr(led, "record_drift"):
        led.record_drift(key_id=key_id_, model=model, media_cls=cls,
                         level=level or DEFAULT_LEVEL.get(cls, ""),
                         predicted=drift["predicted"], measured=drift["measured"],
                         source=source)


def _drift_report_line(cls: str, drift: dict) -> str:
    return (f" · COST DRIFT: {cls} predicted {drift['predicted']} tokens, measured "
            f"{drift['measured']} ({drift['pct']:+.1f}%) — tell the owner the "
            "router's media tariff table is stale and needs re-measuring")


async def _provider_file_analysis(deps: Deps, emitter: ProgressEmitter, params: dict,
                                  inbox: Path, upload_id: str, loaded: _Loaded) -> dict:
    """Provider file: upload and measure; split only a PDF over the FULL minute TPM."""
    filename = loaded.side.get("filename")
    router, ovf, route = _apply_route(deps, params.get("route"), params.get("overflow"))
    model = params.get("model") or router.cfg.chain[0]
    up = _cached_upload(router, loaded)
    level = getattr(deps.app_cfg.ingest, "media_resolution", "") or ""
    if up is not None and level:
        up = up.with_resolution(level)     # a reused reference needs the level just as much
    if up is None:
        await emitter.stage("uploading", 4,
                            f"{filename}: {loaded.nbytes} bytes → provider Files API "
                            f"({loaded.enc})")
        try:
            up = await router.upload_file(
                loaded.path, mime=loaded.enc, model=model, source=_src(params),
                overflow=ovf if loaded.enc == "application/pdf" else "off",
                display_name=filename)
        except QuotaExhaustedError as e:
            raise _fail(deps, emitter, "analyze_file", params.get("request_id"), e) from e
        except RouterError as e:
            raise _fail(deps, emitter, "analyze_file", params.get("request_id"),
                        _UPLOAD_FAILED) from e
        await asyncio.to_thread(ingest.save_provider_ref, inbox, upload_id, {
            "name": up.name, "uri": up.uri, "mime": up.mime,
            "expires_at": up.expires_at, "key_id": up.key_id, "sha256": up.sha256})
    if up.media_resolution is None and level:
        up = up.with_resolution(level)     # fresh uploads get it here, after the upload
    try:
        file_tokens = await router.count_file_tokens(
            model, up.uri, up.mime, key_id=up.key_id,
            media_resolution=up.media_resolution)   # same level the request will use
    except (RouterError, QuotaExhaustedError) as e:      # a measurement, never fatal
        deps.logger.warning("count_file_tokens failed (%s) — sending unmeasured", e)
        file_tokens = 0
    await emitter.stage("uploaded", 8,
                        f"provider file {up.name} ready"
                        + (f" · {file_tokens} tokens" if file_tokens else
                           " · token count unavailable")
                        + (f" ({up.media_resolution})" if up.media_resolution else ""))
    task = params["task"]
    # The TPM gate must include the file: the router refuses a request that carries
    # files without a measured estimate, and this is that measurement.
    est = file_tokens + max(1, len(task) // 4)
    if (up.mime == "application/pdf" and file_tokens and
            est > router.cfg.limits_per_key_model.tpm):
        return await _analyze_split_pdf(deps, emitter, params, loaded, up,
                                        file_tokens, router, ovf, route)
    # Wave 1 drift signal: the router's tariff table predicted the cost before this call;
    # a miss is a note for the owner, never an error — the request ran on the measurement.
    # A pdf with unknown page count has no honest forecast, so it gets no prediction.
    drift, cls = _drift_for(up.mime, loaded.pages, up.media_resolution, file_tokens)
    req = RouterRequest(prompt=task, system=params.get("system"),
                        thinking_level=params.get("thinking_level")
                        or deps.app_cfg.thinking.default,
                        output_format=params.get("output_format", "text"),
                        max_output_tokens=params.get("max_output_tokens"),
                        model=params.get("model"), overflow=ovf,
                        wait=params.get("wait_for_minute_limits"),
                        request_id=params.get("request_id"), source=_src(params),
                        kind="single", est_tokens=est)
    try:
        res = await router.complete(
            req, on_event=emitter.router_event_sync, files=[up],
            file_paths=[loaded.path] if up.mime == "application/pdf" else None)
    except QuotaExhaustedError as e:
        raise _fail(deps, emitter, "analyze_file", req.request_id, e) from e
    served = res.files_used[0] if res.files_used else up
    if served.key_id != up.key_id:
        await asyncio.to_thread(ingest.save_provider_ref, inbox, upload_id, {
            "name": served.name, "uri": served.uri, "mime": served.mime,
            "expires_at": served.expires_at, "key_id": served.key_id,
            "sha256": served.sha256})
    await emitter.stage("done", 100, f"done on {res.model} in {res.latency_s:.1f}s")
    await emitter.drain()
    meta = {"request_id": res.request_id, "model": res.model, "key_id": res.key_id,
            "tokens_in": res.tokens_in, "tokens_out": res.tokens_out,
            "latency_s": res.latency_s, "cached": res.cached,
            "overflow_tier": res.overflow_tier, "quota_after": res.quota_after,
            "route": route, "overflow": ovf, "backend": deps.backend,
            "thinking_level": req.thinking_level, "requests_spent": 0 if res.cached else 1,
            "requested_model": req.model,
            "progress_trace": emitter.trace,
            "file": {"filename": filename, "bytes": loaded.nbytes, "mime": up.mime,
                     "tokens": file_tokens, "resolution": up.media_resolution,
                     "extractor": loaded.extractor, "via": "files_api",
                     "provider": {"name": served.name, "uri": served.uri,
                                  "expires_at": served.expires_at, "key_id": served.key_id}}}
    if drift:
        meta["cost"] = {"drift": drift}
        _record_drift(deps, router, up.key_id, res.model, cls, up.media_resolution, drift)
    _record(deps, "analyze_file", meta, upload_id=upload_id,
            filename=filename, bytes=loaded.nbytes, mime=up.mime,
            file_tokens=file_tokens, via="files_api",
            provider_file=served.name, model=res.model, cached=res.cached)
    report = (f"file={filename} ({loaded.nbytes} bytes"
              + (f", {file_tokens} tokens" if file_tokens else "")
              + (f", {up.media_resolution}" if up.media_resolution else "")
              + f") via Files API · {_report(meta)}")
    if drift:
        report += _drift_report_line(cls, drift)
    return {"report": report, "result": res.text, "meta": meta}


_PDF_MAX_PARTS = 16


def _pdf_split_error(code: str, filename: str, measured: int, limit: int,
                     hint: str, **detail) -> ToolError:
    return ToolError(json.dumps({"error": code, "filename": filename,
                                 "measured_tokens": measured, "tpm_limit": limit,
                                 "hint": hint, **detail}, ensure_ascii=False))


async def _analyze_split_pdf(deps: Deps, emitter: ProgressEmitter, params: dict,
                             loaded: _Loaded, up: UploadedFile, measured: int,
                             router: Router, ovf: str, route: str) -> dict:
    """Full-TPM overflow only. Measure every page-range chunk before generation.

    Each provider request has files=[ONE PDF chunk]. Its answers consolidate to ONE
    per-file answer; the multi-file caller uses only that answer, never the chunks.
    """
    filename = loaded.side.get("filename")
    tpm = router.cfg.limits_per_key_model.tpm
    pages = loaded.pages
    if not pages:
        raise _pdf_split_error("pdf_split_unavailable", filename, measured, tpm,
                               "page count unavailable; cannot safely split PDF")
    if not shutil.which("qpdf"):
        raise _pdf_split_error("pdf_split_unavailable", filename, measured, tpm,
                               "qpdf is not installed; nothing was analyzed")
    task = params["task"]
    # Reserve room for the per-part marker, task, system and request framing.
    overhead = max(512, len(task) // 4 + len(params.get("system") or "") // 4 + 256)
    budget = tpm - overhead
    if budget <= 0:
        raise _pdf_split_error("pdf_split_over_budget", filename, measured, tpm,
                               "task alone leaves no room for a PDF page")
    per_page = max(1, measured / pages)
    span = max(1, int(budget * 0.9 / per_page))
    ranges = [(first, min(pages, first + span - 1))
              for first in range(1, pages + 1, span)]
    if len(ranges) > _PDF_MAX_PARTS:
        raise _pdf_split_error("pdf_split_over_budget", filename, measured, tpm,
                               "more than 16 parts would be needed", parts=len(ranges))
    thinking = params.get("thinking_level") or deps.app_cfg.thinking.default
    pipe = Pipeline(router, deps.app_cfg, emitter, deps.logger)
    ap = AnalyzeParams(task=task, thinking_level=thinking,
                       output_format=params.get("output_format", "text"),
                       max_output_tokens=params.get("max_output_tokens"),
                       model=params.get("model"), overflow=ovf,
                       wait=params.get("wait"), request_id=params.get("request_id"),
                       source=_src(params))
    answers: dict[int, str] = {}
    models: list[str] = []
    hits = spent = 0
    # Files API uploads are streamed; scratch chunk files are deleted on any exit.
    with tempfile.TemporaryDirectory(prefix="gemini-pdf-") as td:
        i = 0
        while i < len(ranges):
            first, last = ranges[i]
            path = Path(td) / f"part-{i + 1}.pdf"
            await emitter.stage("splitting", 10 + 70 * i / len(ranges),
                                f"{filename}: pages {first}-{last} of {pages}")
            try:
                await asyncio.to_thread(split_pages, loaded.path, path, first, last)
            except PdfSplitError as e:
                raise _pdf_split_error("pdf_split_failed", filename, measured, tpm,
                                       str(e), completed_parts=i) from e
            try:
                chunk = await router.upload_file(
                    path, mime="application/pdf",
                    model=params.get("model") or router.cfg.chain[0],
                    key=next(k for k in router.cfg.api_keys if key_id(k) == up.key_id),
                    source=_src(params), display_name=f"{filename} pages {first}-{last}")
                chunk = chunk.with_resolution(up.media_resolution) if up.media_resolution else chunk
                tokens = await router.count_file_tokens(
                    params.get("model") or router.cfg.chain[0], chunk.uri, chunk.mime,
                    key_id=chunk.key_id, media_resolution=chunk.media_resolution)
            except (QuotaExhaustedError, RouterError) as e:
                raise _pdf_split_error("pdf_split_measure_failed", filename,
                                       measured, tpm, f"part {first}-{last} could not be "
                                       "uploaded/measured; no unmeasured request was sent",
                                       completed_parts=i) from e
            # Keep the local chunk until generation ends: key failover may need it
            # for a fresh upload. TemporaryDirectory removes it on any exit.
            if not tokens or tokens + overhead > tpm:
                if first == last or len(ranges) >= _PDF_MAX_PARTS:
                    raise _pdf_split_error("pdf_split_over_budget", filename,
                                           tokens, tpm,
                                           f"part pages {first}-{last} cannot fit; "
                                           "nothing was sent for this part",
                                           completed_parts=i)
                mid = (first + last) // 2
                ranges[i:i+1] = [(first, mid), (mid + 1, last)]
                continue                         # retry a smaller slice; no generate spend
            prompt = split_prompt(task, filename, i + 1, len(ranges),
                                  f"pages {first}-{last} of {pages}")
            req = RouterRequest(
                prompt=prompt, thinking_level=thinking, output_format="text",
                max_output_tokens=params.get("max_output_tokens"),
                model=params.get("model"), overflow=ovf, wait=params.get("wait"),
                request_id=f"{params['request_id']}:pdf{i+1}" if params.get("request_id") else None,
                source=_src(params), kind="chunk", est_tokens=tokens + overhead)
            try:
                res = await router.complete(req, on_event=emitter.router_event_sync,
                                            files=[chunk], file_paths=[path])
            except QuotaExhaustedError as e:
                raise _fail(deps, emitter, "analyze_file", req.request_id, e) from e
            if not (res.text or "").strip():
                raise _pdf_split_error("pdf_split_failed", filename, measured, tpm,
                                       f"empty answer on pages {first}-{last}",
                                       completed_parts=i)
            answers[i+1] = res.text
            models.append(res.model)
            hits += int(res.cached)
            spent += int(not res.cached)
            i += 1
    if len(answers) > 1:
        answer, rounds = await pipe._consolidate(
            ap, answers, [], len(answers), thinking, 4.0,
            deps.app_cfg.pipeline.chunk_size_tokens, emitter.router_event_sync)
    else:
        answer, rounds = next(iter(answers.values())), 0
    await emitter.stage("done", 100, f"{filename}: {len(answers)} PDF parts analyzed")
    await emitter.drain()
    models += pipe._models
    meta = {"request_id": params.get("request_id"), "model_final": models[-1],
            "models_used": models, "thinking_level": thinking, "route": route,
            "requested_model": params.get("model"),
            "overflow": ovf, "backend": deps.backend,
            "requests_spent": spent + pipe._spends,
            "cache_hits": hits + pipe._cache_hits,
            "progress_trace": emitter.trace, "quota_after": router.model_budgets(),
            "file": {"filename": filename, "bytes": loaded.nbytes, "mime": up.mime,
                     "tokens": measured, "resolution": up.media_resolution,
                     "via": "files_api", "split": {
                         "pages": pages, "chunks": len(ranges),
                         "ranges": [[a, b] for a, b in ranges],
                         "resolution": up.media_resolution},
                     "consolidation_rounds": rounds}}
    meta["degradations"] = ["PDF split into page ranges to fit the full minute quota"]
    return {"result": answer, "meta": meta,
            "report": f"file={filename} ({pages} pages, {len(ranges)} parts) · {_report(meta)}"}


def _pack_bins(items: list[dict], budget: int, overhead: int) -> tuple[list, list]:
    """First-fit-decreasing by measured tokens. One pack = one request, so its files
    must fit the budget together with the per-request prompt overhead. Returns
    (packs, refused): non-PDF files above full TPM are refused. PDFs above full
    TPM are removed before this packer and split by _analyze_split_pdf."""
    packs: list[list[dict]] = []
    sizes: list[int] = []
    refused: list[dict] = []
    for it in sorted(items, key=lambda x: -(x["tokens"] or 0)):
        need = (it["tokens"] or 0) + overhead
        if need > budget:
            refused.append(it)
            continue
        for i, pack in enumerate(packs):
            if sizes[i] + need <= budget:
                pack.append(it)
                sizes[i] += need
                break
        else:
            packs.append([it])
            sizes.append(need)
    return packs, refused


async def analyze_files_tool(deps: Deps, emitter: ProgressEmitter, params: dict) -> dict:
    """C2: several files, one answer. Text documents each keep the chunked pipeline;
    binaries are uploaded, token-measured for free, then packed by measured tokens
    against the serving key's minute budget into as few requests as fit (uploads are
    per key, so packs never cross keys). The parts are consolidated into one result.
    A PDF above full TPM is split and consolidated per file before the group merge;
    other files above full TPM are refused with their measured cost."""
    cfgi = deps.app_cfg.ingest
    inbox = Path(cfgi.dir)
    ids: list[str] = list(params.get("upload_ids") or [])
    if not ids:
        raise ToolError(json.dumps({"error": "missing_upload_ids",
                                    "hint": "pass upload_ids from file_upload_ticket"}))
    if len(ids) == 1:                      # one file is the C1 contract, unchanged
        return await analyze_file_tool(deps, emitter, {**params, "upload_id": ids[0]})
    await asyncio.to_thread(ingest.prune, inbox, cfgi.keep_hours, cfgi.total_cap_mb)
    t0 = time.time()
    loadeds: list[tuple[str, _Loaded]] = []
    for i, uid in enumerate(ids, 1):
        await emitter.stage("loading", i * 10 // len(ids),
                            f"loading file {i}/{len(ids)}")
        try:
            loadeds.append(
                (uid, await asyncio.to_thread(_load_and_extract, inbox, uid, cfgi)))
        except ingest.IngestError as e:
            deps.audit.record({"ts": time.time(), "tool": "analyze_files",
                               "upload_id": uid, "error": e.payload["error"]})
            raise ToolError(json.dumps(e.payload, ensure_ascii=False)) from e

    router, ovf, route = _apply_route(deps, params.get("route"), params.get("overflow"))
    level = getattr(cfgi, "media_resolution", "") or ""
    task = params["task"]
    thinking = params.get("thinking_level") or deps.app_cfg.thinking.default
    rid = params.get("request_id")
    pipe = Pipeline(router, deps.app_cfg, emitter, deps.logger)
    ap = AnalyzeParams(task=task, thinking_level=thinking,
                       output_format=params.get("output_format", "text"),
                       max_output_tokens=params.get("max_output_tokens"),
                       model=params.get("model"), overflow=ovf,
                       wait=params.get("wait"), request_id=rid, source=_src(params))
    on_ev = emitter.router_event_sync
    answers: dict[int, str] = {}
    files_meta: list[dict] = []
    drift_lines: list[str] = []
    part_no = 0
    text_spent = text_hits = 0
    group_spent = group_hits = 0
    models_used: list[str] = []

    # -- text documents: keep child progress below the parent job's completion
    n_text = sum(ld.text is not None for _, ld in loadeds)
    text_idx = 0
    for uid, loaded in loadeds:
        if loaded.text is None:
            continue
        filename = loaded.side.get("filename")
        part_no += 1
        await emitter.stage("text_pipeline", 15,
                            f"{filename}: {len(loaded.text)} chars → chunked pipeline")
        sub = {k: v for k, v in params.items() if k not in ("upload_ids", "upload_id")}
        sub["body"] = loaded.text
        sub["request_id"] = f"{rid}:t{part_no}" if rid else None
        with emitter.progress_span(15 + 10 * text_idx / n_text,
                                   15 + 10 * (text_idx + 1) / n_text):
            out = await analyze_large_tool(deps, emitter, sub)
        text_idx += 1
        answers[part_no] = out["result"]
        m = out["meta"]
        text_spent += m.get("requests_spent", 0)
        text_hits += m.get("cache_hits", 0)
        models_used += m.get("models_used") or []
        files_meta.append({"upload_id": uid, "filename": filename, "bytes": loaded.nbytes,
                           "chars": len(loaded.text), "encoding": loaded.enc,
                           "extractor": loaded.extractor, "via": "text"})

    # -- binaries: upload (or reuse), measure for free, then pack by measured tokens
    pdf_in_group = any(ld.text is None and ld.enc == "application/pdf"
                       for _, ld in loadeds)
    bins: list[dict] = []
    for uid, loaded in loadeds:
        if loaded.text is not None:
            continue
        filename = loaded.side.get("filename")
        model = params.get("model") or router.cfg.chain[0]
        up = _cached_upload(router, loaded)
        if up is not None and level:
            up = up.with_resolution(level)
        if up is None:
            await emitter.stage("uploading", 25,
                                f"{filename}: {loaded.nbytes} bytes → provider Files API "
                                f"({loaded.enc})")
            try:
                up = await router.upload_file(
                    loaded.path, mime=loaded.enc, model=model, source=_src(params),
                    overflow=ovf if pdf_in_group else "off", display_name=filename)
            except QuotaExhaustedError as e:
                raise _fail(deps, emitter, "analyze_files", rid, e) from e
            except RouterError as e:
                raise _fail(deps, emitter, "analyze_files", rid, _UPLOAD_FAILED) from e
            await asyncio.to_thread(ingest.save_provider_ref, inbox, uid, {
                "name": up.name, "uri": up.uri, "mime": up.mime,
                "expires_at": up.expires_at, "key_id": up.key_id, "sha256": up.sha256})
        if up.media_resolution is None and level:
            up = up.with_resolution(level)
        try:
            measured = await router.count_file_tokens(
                model, up.uri, up.mime, key_id=up.key_id,
                media_resolution=up.media_resolution)
        except (RouterError, QuotaExhaustedError) as e:      # a measurement, never fatal
            deps.logger.warning("count_file_tokens failed (%s) — packing blind", e)
            measured = 0
        drift, _ = _drift_for(up.mime, loaded.pages, up.media_resolution, measured)
        if drift:
            cls = media_class(up.mime)
            _record_drift(deps, router, up.key_id, model, cls, up.media_resolution, drift)
            drift_lines.append(_drift_report_line(cls, drift))
        bins.append({"upload_id": uid, "filename": filename, "loaded": loaded, "up": up,
                     "tokens": measured, "drift": drift})
        await emitter.stage("measured", 35,
                            f"{filename}: {measured} tokens"
                            + (f" ({up.media_resolution})" if up.media_resolution else ""))

    groups_meta: list[dict] = []
    split_spent = split_hits = 0
    k = 0
    if bins:
        overhead = max(1, len(task) // 4) + 64
        tpm_limit = router.cfg.limits_per_key_model.tpm
        by_key: dict[str, list[dict]] = {}
        oversized_pdfs: list[dict] = []
        for it in bins:
            if (it["up"].mime == "application/pdf" and it["tokens"] and
                    it["tokens"] + overhead > tpm_limit):
                oversized_pdfs.append(it)
            else:
                by_key.setdefault(it["up"].key_id, []).append(it)
        packs: list[list[dict]] = []
        refused: list[dict] = []
        for items in by_key.values():
            # A temporary minute gate is not a full-TPM overflow. Wait it out; do
            # not turn an intact PDF into chunks merely because others spent quota.
            pk, rf = _pack_bins(items, tpm_limit, overhead)
            packs += pk
            refused += rf
        if refused:
            raise ToolError(json.dumps({
                "error": "group_file_over_budget",
                "refused": [{"filename": r["filename"], "measured_tokens": r["tokens"]}
                            for r in refused],
                "hint": "these files alone exceed the key's minute token budget; "
                        "analyze them one at a time with analyze_file"},
                ensure_ascii=False))
        # Refuse all non-splittable over-budget files BEFORE any split generation.
        for split_idx, it in enumerate(oversized_pdfs):
            with emitter.progress_span(
                    35 + 40 * split_idx / len(oversized_pdfs),
                    35 + 40 * (split_idx + 1) / len(oversized_pdfs)):
                out = await _analyze_split_pdf(
                    deps, emitter, params, it["loaded"], it["up"], it["tokens"],
                    router, ovf, route)
            part_no += 1
            answers[part_no] = out["result"]  # ONE per-file answer in final merge
            models_used += out["meta"]["models_used"]
            split_spent += out["meta"]["requests_spent"]
            split_hits += out["meta"]["cache_hits"]
            files_meta.append({"upload_id": it["upload_id"], **out["meta"]["file"]})
        k = len(packs)
        await emitter.stage("packed", 75,
                            f"{len(bins)} binaries → {k} group request(s), "
                            f"{n_text} text doc(s)")
        for gi, pack in enumerate(packs, 1):
            names = ", ".join(it["filename"] for it in pack)
            toks = sum(it["tokens"] for it in pack)
            await emitter.stage("group", 75 + 15 * gi / max(1, k),
                                f"group {gi}/{k}: {len(pack)} file(s), {toks} tokens")
            req = RouterRequest(
                prompt=group_prompt(task, gi, k, names), thinking_level=thinking,
                output_format=ap.output_format, max_output_tokens=ap.max_output_tokens,
                model=ap.model, overflow=ovf, wait=ap.wait,
                request_id=f"{rid}:g{gi}" if rid else None, source=_src(params),
                kind="chunk", est_tokens=toks + overhead)
            try:
                res = await router.complete(
                    req, on_event=on_ev, files=[it["up"] for it in pack],
                    file_paths=([it["loaded"].path for it in pack]
                                if any(it["up"].mime == "application/pdf" for it in pack)
                                else None))
            except QuotaExhaustedError as e:
                raise _fail(deps, emitter, "analyze_files", rid, e) from e
            if not (res.text or "").strip():
                raise RouterError(f"empty answer from {res.model} on group {gi}")
            if res.cached:
                group_hits += 1
            else:
                group_spent += 1
            part_no += 1
            answers[part_no] = res.text
            models_used.append(res.model)
            groups_meta.append({"group": gi,
                                "files": [it["filename"] for it in pack],
                                "tokens": toks, "model": res.model})
            served_files = res.files_used
            for index, it in enumerate(pack):
                served = served_files[index] if served_files else it["up"]
                if served.key_id != it["up"].key_id:
                    await asyncio.to_thread(ingest.save_provider_ref, inbox,
                                            it["upload_id"], {
                        "name": served.name, "uri": served.uri, "mime": served.mime,
                        "expires_at": served.expires_at, "key_id": served.key_id,
                        "sha256": served.sha256})
                files_meta.append({
                    "upload_id": it["upload_id"], "filename": it["filename"],
                    "bytes": it["loaded"].nbytes, "mime": it["up"].mime,
                    "tokens": it["tokens"], "resolution": it["up"].media_resolution,
                    "via": "files_api", "group": gi,
                    "provider": {"name": served.name, "uri": served.uri,
                                 "expires_at": served.expires_at,
                                 "key_id": served.key_id}})

    # -- one final answer from all parts
    rounds = 0
    if len(answers) == 1:
        result = next(iter(answers.values()))
    else:
        result, rounds = await pipe._consolidate(
            ap, answers, [], part_no, thinking, 4.0,
            deps.app_cfg.pipeline.chunk_size_tokens, on_ev)

    await emitter.stage("done", 100, f"done in {time.time() - t0:.1f}s")
    await emitter.drain()
    meta = {"request_id": rid, "models_used": models_used + pipe._models,
            "model_final": (models_used + pipe._models or [None])[-1],
            "thinking_level": thinking, "route": route, "overflow": ovf,
            "requested_model": params.get("model"),
            "backend": deps.backend, "files": files_meta, "groups": groups_meta,
            "parts": part_no, "consolidation_rounds": rounds,
            "requests_spent": pipe._spends + text_spent + group_spent + split_spent,
            "cache_hits": pipe._cache_hits + text_hits + group_hits + split_hits,
            "quota_after": router.model_budgets(),
            "elapsed_s": round(time.time() - t0, 1), "progress_trace": emitter.trace}
    if any("split" in f for f in files_meta):
        meta["degradations"] = ["PDF split into page ranges to fit the full minute quota"]
    drifts = [it["drift"] for it in bins if it.get("drift")]
    if drifts:
        meta["cost"] = {"drift": drifts}
    report = (f"files={len(loadeds)} ({k} group request(s)"
              + (f", {sum(1 for _, ld in loadeds if ld.text is not None)} text doc(s)"
                 if any(ld.text is not None for _, ld in loadeds) else "")
              + f", {part_no} part(s)) · {_report(meta)}")
    report += "".join(drift_lines)
    deps.audit.record({"ts": time.time(), "tool": "analyze_files",
                       "request_id": rid, "upload_ids": ids, "parts": part_no,
                       "groups": k, "requests_spent": meta["requests_spent"],
                       "models_used": meta["models_used"], "elapsed_s": meta["elapsed_s"]})
    return {"report": report, "result": result, "meta": meta}


async def quota_status_tool(deps: Deps) -> dict:
    router = _apply_route(deps, None, None)[0]     # default-route view
    rows = router.quota_table()
    rec = next((r for r in rows if r.get("recommended")), None)
    counters = ("shared ledger (fleet truth)" if deps.backend == "router" else
                "local persistent ledger — accurate on this machine, not shared "
                "across machines (direct mode)")
    return {
        "rows": rows,
        "recommended_model": rec["model"] if rec else None,
        "recommended_key_id": rec["key_id"] if rec else None,
        "why": ("first model in chain order with daily budget left and no minute gate"
                if rec else
                "nothing available without waiting — see rpd_left/wait_s/cooldown_until "
                "and reset_eta_pt"),
        "reset_eta_pt": iso(pt_midnight_ts(deps.router.cfg.quota_day_tz, days_ahead=1)),
        "backend": deps.backend, "counters": counters,
        # Hint rides in the PAYLOAD: Freebuff truncates tool descriptions, but
        # agents reliably call quota_status first and read what comes back.
        "file_flow": FILE_FLOW,
    }


async def router_logs_tool(deps: Deps, n: int = 20, model: str | None = None,
                           status: str | None = None, source: str | None = None,
                           request_id: str | None = None) -> dict:
    return {"calls": deps.router.ledger.calls_tail(
        n, model=model, status=status, source=source, request_id=request_id)}
