"""Compute subcommands: `gemini-mcp ask` and `gemini-mcp analyze`.

The agent-facing surface for environments with no MCP transport (OmniRush disables
MCP outright). Parity rule: these call the SAME `*_tool()` functions the MCP server
calls — no second implementation of routing, chunking, quota or reporting lives here.
What this module owns is only the shell contract:

* stdout carries data, stderr carries everything else;
* one envelope shape for success and failure, so a caller never parses prose;
* a stable exit-code taxonomy, so an agent can tell "wait and retry" from "never
  retry this" without reading the message.

Bodies never travel through argv (visible in `ps`/history, length-limited by
ARG_MAX): they come from stdin or `--prompt-file`/`--body-file`. That also keeps a
large body out of the calling agent's context — the point of the whole gateway.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from rich.console import Console

from .config import AppCfg
from .logging_setup import redact
from .progress import ProgressEmitter
from .tools import (
    Deps,
    analyze_file_tool,
    analyze_large_tool,
    gemini_generate_tool,
)

out = Console(stderr=False, highlight=False)
err = Console(stderr=True, highlight=False)

# Error payload code → exit status. Mirrors the families in server._TOOL_ERROR_STATUS
# but collapses HTTP statuses into what a CALLER can act on: whether re-running could
# ever help. Keep the two tables in step — a new error code must land in both.
EXIT_OK = 0
EXIT_USAGE = 2          # the invocation is wrong; the same command never works
EXIT_NOT_FOUND = 3      # referenced state is gone (upload, job)
EXIT_CONSENT = 4        # needs an explicit human yes before it will run
EXIT_QUOTA = 5          # nothing wrong with the call; try later
EXIT_REFUSED = 6        # input refused by shape or size; change the input
EXIT_UPSTREAM = 7       # provider/transport failed after the router's own retries

_EXIT_BY_CODE = {
    # quota — retryable later, and the ONLY family where waiting helps
    "quota_exhausted": EXIT_QUOTA,
    "plan_budget_insufficient": EXIT_QUOTA,
    # usage
    "missing_prompt": EXIT_USAGE,
    "bad_json": EXIT_USAGE,
    "bad_size": EXIT_USAGE,
    "missing_upload_ids": EXIT_USAGE,
    # consent
    "confirm_required": EXIT_CONSENT,
    # not found / stale
    "upload_not_found": EXIT_NOT_FOUND,
    "ticket_expired": EXIT_NOT_FOUND,
    "ticket_consumed": EXIT_NOT_FOUND,
    "already_uploaded": EXIT_NOT_FOUND,
    # refused by shape or size
    "raw_document_inlined": EXIT_REFUSED,
    "file_too_large": EXIT_REFUSED,
    "pdf_too_large": EXIT_REFUSED,
    "pdf_too_many_pages": EXIT_REFUSED,
    "media_too_large": EXIT_REFUSED,
    "size_mismatch": EXIT_REFUSED,
    "binary_file_needs_conversion": EXIT_REFUSED,
    "conversion_unavailable": EXIT_REFUSED,
    "files_api_disabled": EXIT_REFUSED,
    "bad_signature": EXIT_REFUSED,
    "group_file_over_budget": EXIT_REFUSED,
    # upstream
    "file_upload_failed": EXIT_UPSTREAM,
    "ingest_disabled": EXIT_UPSTREAM,
    "internal": EXIT_UPSTREAM,
    "job_failed": EXIT_UPSTREAM,
    "unauthorized": EXIT_UPSTREAM,
}

# What `meta` keeps by default. The full trace is available with --fields all; the
# point of the short list is that a routine call returns a routine-sized payload.
_DEFAULT_META_FIELDS = ("models_used", "requests_spent", "cache_hits", "quota_after",
                        "elapsed_s", "chunks", "cost", "file", "files", "groups")


def _emit(payload: dict, *, as_json: bool, exit_code: int = EXIT_OK) -> int:
    """One object on stdout, then return the exit code (never sys.exit here).

    Returning instead of exiting matters: on a non-blocking pipe a process teardown
    can beat the stdout drain and truncate the JSON. The caller sets the status and
    lets the interpreter flush.
    """
    if as_json:
        out.print_json(json.dumps(payload, ensure_ascii=False, default=str))
    elif payload.get("ok"):
        out.print(payload.get("report") or "", highlight=False)
        out.print(payload.get("result") or "", highlight=False)
    else:
        e = payload.get("error") or {}
        err.print(f"[red]{e.get('code') or 'error'}[/red]: {e.get('message') or payload}",
                  highlight=False)
        if e.get("fix"):
            err.print(f"[yellow]fix:[/yellow] {e['fix']}", highlight=False)
    return exit_code


def _error_payload(code: str, message: str, *, fix: str = "", **extra) -> dict:
    return {"ok": False,
            "error": {"code": code, "message": message, "fix": fix, **extra}}


def _from_tool_error(exc: Exception) -> tuple[dict, int]:
    """A ToolError carries a JSON payload; anything else is an unexpected failure."""
    raw = str(exc)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict) or "error" not in payload:
        return (_error_payload("internal", redact(raw)[:500],
                               fix="unexpected failure — run 'gemini-mcp doctor'"),
                EXIT_UPSTREAM)
    code = str(payload["error"])
    # Everything the core put in the payload rides along — including its own `fix`,
    # which is why the reserved names are filtered out instead of re-passed.
    body = {k: v for k, v in payload.items()
            if k not in ("error", "agent_notice", "progress_trace", "code", "message",
                         "fix")}
    return (_error_payload(code, payload.get("hint") or payload.get("fix") or code,
                           **({"fix": payload["fix"]} if payload.get("fix") else {}),
                           **body),
            _EXIT_BY_CODE.get(code, EXIT_UPSTREAM))


def _slim_meta(meta: dict, fields: str | None) -> dict:
    if not fields:
        return {k: v for k, v in meta.items() if k in _DEFAULT_META_FIELDS}
    if fields.strip().lower() in ("all", "*"):
        return meta
    wanted = [f.strip() for f in fields.split(",") if f.strip()]
    return {k: meta[k] for k in wanted if k in meta}


def _read_text(path: str | None, *, what: str) -> tuple[str | None, dict | None]:
    """Load a body from a path. Returns (text, error_payload)."""
    if not path:
        return None, None
    p = Path(path).expanduser()
    if not p.is_file():
        return None, _error_payload(
            "bad_size", f"{what} not found: {p}",
            fix="check the path, or pipe the text in on stdin")
    try:
        return p.read_text(encoding="utf-8"), None
    except UnicodeDecodeError as e:
        return None, _error_payload(
            "binary_file_needs_conversion",
            f"{what} is not UTF-8 text ({p.name}): {e.reason}",
            fix="for PDF/DOCX/RTF use 'gemini-mcp analyze -f <path>' once the file "
                "lane lands, or convert to plain text first")


def _body_from_stdin() -> str:
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    return sys.stdin.read()


def _common_kwargs(args) -> dict:
    """The one parameter dictionary shared by ask and analyze — a single vocabulary,
    not a per-command dialect.

    `wait` is spelled as the core spells it: AnalyzeParams takes `wait`, while the
    MCP-facing name is `wait_for_minute_limits` and server.py renames it on the way
    in. The CLI has to do the same renaming or the pipeline rejects the dict.
    """
    return {"thinking_level": args.thinking,
            "output_format": args.format,
            "max_output_tokens": args.max_output_tokens,
            "model": args.model,
            "overflow": args.overflow,
            "route": args.route,
            "wait": args.wait_for_minute_limits,
            "request_id": args.request_id}


async def _call(deps: Deps, runner, params: dict) -> dict:
    emitter = ProgressEmitter(None, deps.logger)   # ctx=None → trace only, no MCP
    return await runner(deps, emitter, params)


def _wants_json(args) -> bool:
    """Explicit flag wins; otherwise JSON whenever stdout is not a terminal, so an
    agent gets structure without having to remember the flag."""
    return bool(args.json) or not sys.stdout.isatty()


def _finish(res: dict, args) -> int:
    return _emit({"ok": True, "report": res.get("report", ""),
                  "result": res.get("result", ""),
                  "meta": _slim_meta(res.get("meta") or {}, args.fields)},
                 as_json=_wants_json(args))


def run_ask(deps: Deps, app_cfg: AppCfg, args) -> int:
    prompt, load_err = _read_text(args.prompt_file, what="prompt file")
    if load_err:
        return _emit(load_err, as_json=_wants_json(args), exit_code=EXIT_USAGE)
    prompt = prompt if prompt is not None else (args.prompt or _body_from_stdin())
    if not prompt.strip():
        return _emit(_error_payload(
            "missing_prompt", "no prompt given",
            fix="pass --prompt '…', --prompt-file <path>, or pipe text on stdin"),
            as_json=_wants_json(args), exit_code=EXIT_USAGE)
    kw = _common_kwargs(args)
    # gemini_generate reads the MCP-facing name, analyze_large the core name. Both
    # spellings are mapped here, exactly as server.py maps them for each tool.
    kw["wait_for_minute_limits"] = kw.pop("wait")
    params = {**kw, "prompt": prompt, "system": args.system, "source": "cli"}
    try:
        res = asyncio.run(_call(deps, gemini_generate_tool, params))
    except Exception as e:  # noqa: BLE001 — the CLI answers with a code, not a trace
        payload, code = _from_tool_error(e)
        return _emit(payload, as_json=_wants_json(args), exit_code=code)
    return _finish(res, args)


def run_analyze(deps: Deps, app_cfg: AppCfg, args) -> int:
    if args.file:
        # The file lane (ticket → upload → provider) is a separate plan step: in fleet
        # mode the bytes have to reach the VPS, and that transport is not wired into
        # the CLI yet. Refuse plainly rather than pretend.
        return _emit(_error_payload(
            "not_supported",
            "-f/--file is not wired into the CLI yet",
            fix="use the MCP tools file_upload_ticket + analyze_file for now; the CLI "
                "file lane is a tracked plan step"),
            as_json=_wants_json(args), exit_code=EXIT_USAGE)
    if not args.task:
        return _emit(_error_payload("missing_prompt", "no task given",
                                    fix="pass --task 'what to do with the body'"),
                     as_json=_wants_json(args), exit_code=EXIT_USAGE)
    body, load_err = _read_text(args.body_file, what="body file")
    if load_err:
        return _emit(load_err, as_json=_wants_json(args), exit_code=EXIT_USAGE)
    body = body if body is not None else _body_from_stdin()
    if not body.strip():
        return _emit(_error_payload(
            "missing_prompt", "no body given",
            fix="pass --body-file <path> or pipe the text on stdin; the body is never "
                "taken from argv"),
            as_json=_wants_json(args), exit_code=EXIT_USAGE)
    params = {**_common_kwargs(args), "task": args.task, "body": body, "source": "cli"}
    try:
        res = asyncio.run(_call(deps, analyze_large_tool, params))
    except Exception as e:  # noqa: BLE001
        payload, code = _from_tool_error(e)
        return _emit(payload, as_json=_wants_json(args), exit_code=code)
    return _finish(res, args)


def run_analyze_file(deps: Deps, app_cfg: AppCfg, args) -> int:
    """`analyze --upload-id`: analyze something already uploaded through the MCP flow.

    This half of the file lane needs no new transport — the bytes are already on the
    server — so it is available to the CLI today and keeps the two interfaces at
    parity for anything uploaded by either of them.
    """
    if not args.task:
        return _emit(_error_payload("missing_prompt", "no task given",
                                    fix="pass --task 'what to do with the file'"),
                     as_json=_wants_json(args), exit_code=EXIT_USAGE)
    params = {**_common_kwargs(args), "task": args.task,
              "upload_id": args.upload_id, "source": "cli"}
    try:
        res = asyncio.run(_call(deps, analyze_file_tool, params))
    except Exception as e:  # noqa: BLE001
        payload, code = _from_tool_error(e)
        return _emit(payload, as_json=_wants_json(args), exit_code=code)
    return _finish(res, args)
