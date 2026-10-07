"""MCP server (SDK v2: MCPServer) — one codebase, two transports (FR-B9):
stdio (over SSH) and streamable HTTP (localhost daemon, bearer token).
Custom routes expose REST /v1/quota + /v1/complete for non-MCP consumers (BR4)."""
from __future__ import annotations

import hmac
import json
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
    analyze_large_tool,
    gemini_generate_tool,
    quota_status_tool,
    router_logs_tool,
)

INSTRUCTIONS = """\
Gemini gateway for the Google AI Studio FREE tier (Flash chain 3.8→3.7→3.6→3.5→3,
each ~20 requests/day). Use analyze_large for massive bodies: it measures tokens,
splits into overlapping chunks (each marked 'CHUNK N/M'), analyzes them, consolidates,
and returns ONLY the compact result plus meta (models, spends, quota_after,
progress_trace) — your context stays clean. thinking_level defaults to 'high';
wait_for_minute_limits defaults to true (the server waits out RPM/TPM windows and
reports a countdown via progress). Daily-quota exhaustion is a HARD STOP: you receive
a structured quota_exhausted / plan_budget_insufficient error with per-model budgets,
wait times and reset ETA — then retry later, shrink the job, or opt in with
overflow='lite' or 'lite+gemma' (weaker models with large quotas). Call quota_status
BEFORE big jobs; router_logs shows recent spend."""


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
        wait_for_minute_limits: bool | None = None,
        chunk_tokens: int | None = None,
        overlap_ratio: float | None = None,
        request_id: str | None = None,
        result_max_chars: int | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Analyze a massive text body with Gemini without polluting your context.

        The server measures the body, and if it exceeds the input budget it splits it
        into overlapping chunks (each sent with a 'CHUNK N/M' service note), analyzes
        every chunk separately, then consolidates all answers (recursively if needed)
        into one compact result. Quota is pre-checked: if the plan (chunks + 1
        consolidation) does not fit the remaining daily budget, the call fails fast
        with 'plan_budget_insufficient' BEFORE spending anything.

        Args:
            task: what to do with the body (analysis instructions for the model).
            body: the massive text to analyze.
            thinking_level: 'low'|'medium'|'high' (default: high).
            output_format: 'text' or 'json' for the final consolidated result.
            max_output_tokens: cap per model call (chunks and consolidation).
            model: pin one model (skips the fallback chain).
            overflow: opt-in weaker models when Flash quotas are exhausted:
                'off' (default, hard stop) | 'lite' | 'lite+gemma'.
            wait_for_minute_limits: wait out RPM/TPM windows with a progress countdown
                (default true); false → route to the next model instantly or return
                wait ETAs in the error.
            chunk_tokens: override chunk size (tokens).
            overlap_ratio: override chunk overlap (0..0.5).
            request_id: idempotency key — repeating it returns the cached result.
            result_max_chars: cap on the returned consolidated text.

        Returns: {result, meta{models_used, chunks{count,failed}, requests_spent,
        cache_hits, quota_after, elapsed_s, truncated, progress_trace}}.
        Errors: quota_exhausted / plan_budget_insufficient — JSON with per_model
        budgets, wait seconds and reset_eta_pt.
        """
        emitter = ProgressEmitter(ctx, deps.logger)
        return await analyze_large_tool(deps, emitter, {
            "task": task, "body": body, "thinking_level": thinking_level,
            "output_format": output_format, "max_output_tokens": max_output_tokens,
            "model": model, "overflow": overflow, "wait": wait_for_minute_limits,
            "chunk_tokens": chunk_tokens, "overlap_ratio": overlap_ratio,
            "request_id": request_id, "result_max_chars": result_max_chars})

    @srv.tool()
    async def gemini_generate(
        prompt: str,
        system: str | None = None,
        thinking_level: Literal["low", "medium", "high"] | None = None,
        output_format: Literal["text", "json"] = "text",
        max_output_tokens: int | None = None,
        model: str | None = None,
        overflow: Literal["off", "lite", "lite+gemma"] = "off",
        wait_for_minute_limits: bool | None = None,
        request_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Single-shot quota-aware Gemini completion (payload fits the limits).

        Same routing, thinking_level (default high), wait policy and overflow semantics
        as analyze_large, without chunking. Returns {result, meta{model, key_id,
        tokens_in/out, latency_s, cached, quota_after, progress_trace}}.
        """
        emitter = ProgressEmitter(ctx, deps.logger)
        return await gemini_generate_tool(deps, emitter, {
            "prompt": prompt, "system": system, "thinking_level": thinking_level,
            "output_format": output_format, "max_output_tokens": max_output_tokens,
            "model": model, "overflow": overflow,
            "wait_for_minute_limits": wait_for_minute_limits, "request_id": request_id})

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
        if path == "/healthz" or not any(path.startswith(p) for p in self.protected):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or ())
        auth = headers.get(b"authorization", b"").decode("latin-1")
        ok = auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], self.token)
        if not ok:
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
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
    return app
