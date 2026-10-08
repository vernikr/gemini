"""MCP server (SDK v2: MCPServer) — one codebase, two transports (FR-B9):
stdio (over SSH) and streamable HTTP (localhost daemon, bearer token).
Custom routes expose REST /v1/quota + /v1/complete for non-MCP consumers (BR4)."""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .config import AppCfg
from .progress import ProgressEmitter
from .tools import (
    Deps,
    _jobbed,
    analyze_file_tool,
    analyze_large_tool,
    analyze_result_tool,
    file_upload_ticket_tool,
    gemini_generate_tool,
    quota_status_tool,
    router_logs_tool,
)

INSTRUCTIONS = """\
Gemini gateway for the Google AI Studio FREE tier (Flash models, ~20 requests/day
each). Two routing profiles ('route' argument of the compute tools):
- 'optimal' (default): models are tried in empirical availability order
  3.6 → 3.5 → 3-preview → 3.8 → 3.7 (older = less loaded on the free tier);
  overflow to flash-lite/gemma is AUTOMATIC when Flash budgets run out.
- 'best_available': newest-first 3.8 → 3.7 → 3.6 → 3.5 → 3-preview; overflow is
  strictly opt-in ('off' = HARD STOP on quota exhaustion).
Use analyze_large for massive bodies: it measures tokens, splits into overlapping
chunks (each marked 'CHUNK N/M'), analyzes them, consolidates, and returns ONLY the
compact result plus meta (models, spends, quota_after, progress_trace) — your context
stays clean. thinking_level defaults to 'medium'; wait_for_minute_limits defaults to
true (the server waits out RPM/TPM windows and reports a countdown via progress).
On a hard stop you receive a structured quota_exhausted / plan_budget_insufficient
error with per-model budgets, wait times and reset ETA — then retry later, shrink the
job, or opt in with overflow='lite'/'lite+gemma'. Call quota_status BEFORE big jobs;
router_logs shows recent spend.

TIMEOUT-SAFE LONG JOBS: analyze_large/gemini_generate hold the call inline for at most
wait_seconds (default 25). If the job needs longer, the call returns
{status:'running', request_id, poll} — the server KEEPS COMPUTING. Then call
analyze_result(request_id) (each call waits up to its own wait_seconds) until you get
the final {result, meta}. Never resubmit a running job: quota is already committed.

REPORTING (required): every analyze_large/gemini_generate/analyze_file response
starts with a one-line 'report' field: route · thinking level · final model ·
requests spent · 'clean run' or rough_edges count. ALWAYS relay that line to the
user in chat verbatim; if rough_edges > 0, say in plain words what happened
(provider 503s and retries, model fallbacks, minute-limit waits with seconds,
failed chunks — details are in meta.progress_trace). If the call errored, say so
plainly with the reset ETA. Never silently swallow retries, waits or fallbacks.

FILE INGEST: to analyze a local file — file_upload_ticket(filename, size_bytes) →
upload the bytes with ONE command (curl -T <file> '<upload_url>' through the :8790
tunnel; the signed URL IS the auth, or scp <file> <scp_path> without the tunnel) →
analyze_file(task, upload_id, …). The server extracts text itself (RTF supported
server-side); binaries fail fast with ZERO quota spent."""


def build_server(deps: Deps) -> MCPServer:
    srv = MCPServer("gemini", instructions=INSTRUCTIONS)

    @srv.tool()
    async def analyze_large(
        task: str,
        body: str,
        thinking_level: Literal["low", "medium", "high"] | None = None,
        output_format: Literal["text", "json"] = "text",
        max_output_tokens: int | None = None,
        model: str | None = None,
        overflow: Literal["off", "lite", "lite+gemma"] = "off",
        route: Literal["optimal", "best_available"] | None = None,
        wait_for_minute_limits: bool | None = None,
        chunk_tokens: int | None = None,
        overlap_ratio: float | None = None,
        request_id: str | None = None,
        result_max_chars: int | None = None,
        wait_seconds: int | None = 25,
        ctx: Context | None = None,
    ) -> dict:
        """Analyze a massive text body with Gemini without polluting your context.
        ALWAYS relay the response's 'report' field (route · thinking · model · spend
        · rough edges) to the user in chat.

        The server measures the body, and if it exceeds the input budget it splits it
        into overlapping chunks (each sent with a 'CHUNK N/M' service note), analyzes
        every chunk separately, then consolidates all answers (recursively if needed)
        into one compact result. Quota is pre-checked: if the plan (chunks + 1
        consolidation) does not fit the remaining daily budget, the call fails fast
        with 'plan_budget_insufficient' BEFORE spending anything.

        Args:
            task: what to do with the body (analysis instructions for the model).
            body: the massive text to analyze.
            thinking_level: 'low'|'medium'|'high' (default: medium).
            output_format: 'text' or 'json' for the final consolidated result.
            max_output_tokens: cap per model call (chunks and consolidation).
            model: pin one model (skips the fallback chain).
            overflow: weaker models when Flash quotas are exhausted:
                'off' | 'lite' | 'lite+gemma'. On route 'optimal' it is
                auto-upgraded to 'lite+gemma' unless you set it explicitly.
            route: 'optimal' (default) — availability-ordered chain
                3.6→3.5→3-preview→3.8→3.7 with automatic overflow; or
                'best_available' — newest-first chain, overflow opt-in only
                (hard stop on quota).
            wait_for_minute_limits: wait out RPM/TPM windows with a progress countdown
                (default true); false → route to the next model instantly or return
                wait ETAs in the error.
            chunk_tokens: override chunk size (tokens).
            overlap_ratio: override chunk overlap (0..0.5).
            request_id: idempotency key — repeating it returns the cached result;
                also the handle for the async ticket flow below.
            result_max_chars: cap on the returned consolidated text.
            wait_seconds: max seconds to hold this call inline (default 25). Longer
                jobs return {status:'running', request_id} and keep computing
                server-side — fetch the outcome with analyze_result(request_id).
                None = block until finished (legacy behavior, risks client timeouts).

        Returns: {report, result, meta{models_used, chunks{count,failed},
        requests_spent, cache_hits, quota_after, elapsed_s, truncated,
        progress_trace}} or {status:'running', request_id, elapsed_s, poll} while
        the job computes.
        Errors: quota_exhausted / plan_budget_insufficient — JSON with per_model
        budgets, wait seconds and reset_eta_pt.
        """
        emitter = ProgressEmitter(ctx, deps.logger)
        return await _jobbed(deps, "analyze_large", emitter, {
            "task": task, "body": body, "thinking_level": thinking_level,
            "output_format": output_format, "max_output_tokens": max_output_tokens,
            "model": model, "overflow": overflow, "route": route,
            "wait": wait_for_minute_limits,
            "chunk_tokens": chunk_tokens, "overlap_ratio": overlap_ratio,
            "request_id": request_id, "result_max_chars": result_max_chars},
            wait_seconds,
            lambda em, pr: analyze_large_tool(deps, em, pr))

    @srv.tool()
    async def gemini_generate(
        prompt: str,
        system: str | None = None,
        thinking_level: Literal["low", "medium", "high"] | None = None,
        output_format: Literal["text", "json"] = "text",
        max_output_tokens: int | None = None,
        model: str | None = None,
        overflow: Literal["off", "lite", "lite+gemma"] = "off",
        route: Literal["optimal", "best_available"] | None = None,
        wait_for_minute_limits: bool | None = None,
        request_id: str | None = None,
        wait_seconds: int | None = 25,
        ctx: Context | None = None,
    ) -> dict:
        """Single-shot quota-aware Gemini completion (payload fits the limits).
        ALWAYS relay the response's 'report' field to the user in chat.

        Same routing profiles ('route': 'optimal' default / 'best_available'),
        thinking_level (default medium), wait policy, overflow semantics and async
        ticket flow (wait_seconds, default 25 → {status:'running', request_id} +
        analyze_result) as analyze_large, without chunking. Returns {report, result,
        meta{model, key_id, tokens_in/out, latency_s, cached, route, quota_after,
        progress_trace}}.
        """
        emitter = ProgressEmitter(ctx, deps.logger)
        return await _jobbed(deps, "gemini_generate", emitter, {
            "prompt": prompt, "system": system, "thinking_level": thinking_level,
            "output_format": output_format, "max_output_tokens": max_output_tokens,
            "model": model, "overflow": overflow, "route": route,
            "wait_for_minute_limits": wait_for_minute_limits,
            "request_id": request_id},
            wait_seconds,
            lambda em, pr: gemini_generate_tool(deps, em, pr))

    @srv.tool()
    async def analyze_result(
        request_id: str,
        wait_seconds: int | None = 25,
        ctx: Context | None = None,
    ) -> dict:
        """Outcome of a background job (analyze_large/gemini_generate returned
        {status:'running', request_id}). Each call waits up to wait_seconds.

        Returns: the final {result, meta} when done; {status:'running', elapsed_s}
        while it still computes (call again); {status:'unknown', hint} if the daemon
        restarted — re-run the ORIGINAL call with the SAME request_id (finished
        chunks come from the router cache, so the retry is cheap). Quota/plan errors
        surface exactly as in the sync path (structured JSON)."""
        return await analyze_result_tool(deps, request_id, wait_seconds)

    @srv.tool()
    async def file_upload_ticket(
        filename: str,
        size_bytes: int,
        ctx: Context | None = None,
    ) -> dict:
        """Reserve a file upload (step 1 of 2). Returns {upload_id, upload_url,
        scp_path?, expires_in_s, instructions}.

        upload_url is an HMAC one-time link (15 min, bound to declared size, no
        bearer needed): upload with ONE command — `curl -T <file> '<upload_url>'`
        through the :8790 tunnel — or `scp <file> <scp_path>` without any tunnel.
        Then call analyze_file(task, upload_id) (step 2). ANY file can be uploaded;
        the SERVER decides split/convert: text-like files are analyzed, RTF is
        extracted server-side, binaries fail fast with zero quota spent."""
        return await file_upload_ticket_tool(deps, filename, size_bytes)

    @srv.tool()
    async def analyze_file(
        task: str,
        upload_id: str,
        thinking_level: Literal["low", "medium", "high"] | None = None,
        output_format: Literal["text", "json"] = "text",
        max_output_tokens: int | None = None,
        model: str | None = None,
        overflow: Literal["off", "lite", "lite+gemma"] = "off",
        route: Literal["optimal", "best_available"] | None = None,
        wait_for_minute_limits: bool | None = None,
        chunk_tokens: int | None = None,
        overlap_ratio: float | None = None,
        request_id: str | None = None,
        result_max_chars: int | None = None,
        wait_seconds: int | None = 25,
        ctx: Context | None = None,
    ) -> dict:
        """Analyze an uploaded file server-side (step 2 of 2; D11: zero client-side
        processing). ALWAYS relay the response's 'report' field to the user in chat.

        The server loads inbox/<upload_id>, sniffs it (binary → fail-fast
        'binary_file_needs_conversion', ZERO quota spent), extracts text (RTF
        supported server-side; txt/md/csv as-is), then runs the full analyze_large
        pipeline on it. Same knobs, routes and async ticket flow as analyze_large.

        Returns: {report, result, meta{…, file{filename, bytes, chars, encoding,
        extractor}}} or {status:'running', request_id} while the job computes."""
        emitter = ProgressEmitter(ctx, deps.logger)
        return await _jobbed(deps, "analyze_file", emitter, {
            "task": task, "upload_id": upload_id, "thinking_level": thinking_level,
            "output_format": output_format, "max_output_tokens": max_output_tokens,
            "model": model, "overflow": overflow, "route": route,
            "wait": wait_for_minute_limits, "chunk_tokens": chunk_tokens,
            "overlap_ratio": overlap_ratio, "request_id": request_id,
            "result_max_chars": result_max_chars},
            wait_seconds,
            lambda em, pr: analyze_file_tool(deps, em, pr))

    @srv.tool()
    async def quota_status(ctx: Context | None = None) -> dict:
        """Live free-tier budget: per key×model RPD left, RPM/TPM window state,
        cooldowns, the recommended target for the next request and the PT reset ETA.
        Call this BEFORE large jobs to plan chunk cost against the remaining budget."""
        return await quota_status_tool(deps)

    @srv.tool()
    async def router_logs(
        n: int = 20,
        model: str | None = None,
        status: str | None = None,
        source: str | None = None,
        request_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Audit-log tail of Gemini calls (ts, source, kind, key_id, model, status,
        tokens, latency, overflow_tier). Filters: model/status/source/request_id.
        Statuses: ok|quota|http4xx|http5xx|network|blocked|skipped|planned."""
        return await router_logs_tool(deps, n=n, model=model, status=status,
                                      source=source, request_id=request_id)

    @srv.resource("quota://status")
    async def quota_resource() -> str:
        return json.dumps(await quota_status_tool(deps), ensure_ascii=False, default=str)

    @srv.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "service": "gemini-mcp", "version": __version__})

    @srv.custom_route("/v1/quota", methods=["GET"])
    async def v1_quota(request: Request) -> JSONResponse:
        return JSONResponse(json.loads(json.dumps(await quota_status_tool(deps),
                                                  default=str)))

    @srv.custom_route("/v1/complete", methods=["POST"])
    async def v1_complete(request: Request) -> JSONResponse:
        data = await request.json()
        emitter = ProgressEmitter(None, deps.logger)
        from mcp.server.mcpserver.exceptions import ToolError
        try:
            out = await gemini_generate_tool(deps, emitter, data)
        except ToolError as e:
            return JSONResponse({"error": str(e)}, status_code=429)
        return JSONResponse(json.loads(json.dumps(out, default=str)))

    @srv.custom_route("/v1/upload/{upload_id}", methods=["PUT", "POST"])
    async def upload(request: Request) -> JSONResponse:
        """Presigned one-time upload (M4.5 §9): the HMAC signature in the query IS
        the auth (bearer-exempt); size-bound, TTL-bound, single use."""
        from .ingest import IngestError, save_upload, verify_ticket

        cfgi = deps.app_cfg.ingest
        token = os.environ.get("GEMINI_MCP_TOKEN", "")
        q = request.query_params
        uid = request.path_params.get("upload_id", "")
        try:
            verify_ticket(token, uid, q.get("exp", ""), q.get("size", ""),
                          q.get("sig", ""))
            data = await request.body()
            path = save_upload(Path(cfgi.dir), uid, data,
                               cfgi.max_file_mb * 1_048_576)
        except IngestError as e:
            code = {"ticket_expired": 410, "already_uploaded": 409,
                    "ingest_disabled": 503}.get(e.payload["error"], 400)
            return JSONResponse(e.payload, status_code=code)
        deps.logger.info("ingest: stored %s (%d bytes)", uid, path.stat().st_size)
        return JSONResponse({"ok": True, "upload_id": uid, "bytes": len(data),
                             "next": f"call analyze_file(task=…, upload_id='{uid}')"})

    return srv


class BearerMiddleware:
    """Pure-ASGI bearer guard (FR-F4): protects mcp_path + /v1, leaves /healthz open.
    Token compared in constant time; keys/tokens never logged."""

    def __init__(self, app, token: str, protected: tuple[str, ...] = ("/mcp", "/v1")):
        self.app = app
        self.token = token
        self.protected = protected

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/healthz" or path.startswith("/v1/upload/") \
                or not any(path.startswith(p) for p in self.protected):
            # /v1/upload/* carries its own HMAC one-time auth (ticket signature)
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or ())
        auth = headers.get(b"authorization", b"").decode("latin-1")
        ok = auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], self.token)
        if not ok:
            response = JSONResponse(
                {"error": "unauthorized",
                 "error_description": "static bearer auth: send 'Authorization: Bearer "
                                      "<GEMINI_MCP_TOKEN>' (no OAuth on this server)"},
                status_code=401, headers={"WWW-Authenticate": 'Bearer realm="gemini-mcp"'})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class AuthMetadataRoutes:
    """OAuth-discovery endpoints (RFC 9728/8414) telling MCP clients that this is
    STATIC bearer auth with no authorization server.

    Live finding (2026-10-08, Freebuff): after an unauthenticated probe gets 401, the
    client walks the discovery ladder — /.well-known/oauth-protected-resource, then
    oauth-authorization-server, then OIDC /.well-known/openid-configuration; Starlette's
    default 404 (text/plain 'Not Found') crashed the client's JSON parser ('Unexpected
    identifier Not'). We serve RFC 9728 metadata on protected-resource paths and JSON
    (never text) on EVERY other /.well-known/* path."""

    def __init__(self, app, mcp_path: str = "/mcp"):
        self.app = app
        self.mcp_path = mcp_path

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path.startswith("/.well-known/") or path in ("/register", "/authorize", "/token") \
                or path.startswith("/oauth"):
            headers = dict(scope.get("headers") or ())
            host = headers.get(b"host", b"localhost").decode("latin-1")
            if path.rstrip("/") in ("/.well-known/oauth-protected-resource",
                                    f"/.well-known/oauth-protected-resource{self.mcp_path}"):
                resp = JSONResponse({
                    "resource": f"http://{host}{self.mcp_path}",
                    "bearer_methods_supported": ["header"],
                    "resource_documentation": "https://github.com/vernikr/gemini-mcp",
                    "tls_client_certificate_bound_access_tokens": False})
            else:   # authorization-server meta, DCR /register, /authorize, /token,
                # OIDC: JSON 404, never text/plain (clients JSON-parse every rung)
                resp = JSONResponse(
                    {"error": "not_supported",
                     "error_description": "static bearer token auth; no authorization server"},
                    status_code=404)
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_http_app(srv: MCPServer, cfg: AppCfg, token: str | None):
    app = srv.streamable_http_app(
        streamable_http_path=cfg.server.mcp_path,
        host=cfg.server.http_bind,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    if token:
        app = BearerMiddleware(app, token, protected=(cfg.server.mcp_path, "/v1"))
    return AuthMetadataRoutes(app, cfg.server.mcp_path)
