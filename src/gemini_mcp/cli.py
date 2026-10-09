"""gemini-mcp CLI: serve | stdio | quota | calls | logs | prune | doctor.
Terminal observability (FR-E7) works over SSH against the shared state without
touching the daemon."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from gemini_router.cli import cmd_calls, cmd_doctor, cmd_quota
from rich.console import Console

from . import __version__
from .backends import make_routers, resolve_mode
from .config import AppCfg, load_all
from .logging_setup import AuditLogger, enforce_audit_size, prune_jsonl, redact, setup_logging
from .tools import Deps

console = Console(stderr=True)


def _deps(app_cfg: AppCfg, router_cfg) -> Deps:
    logger = setup_logging(app_cfg.logging,
                           secrets=[os.environ.get("GEMINI_MCP_TOKEN", "")])
    router, router_optimal, mode = make_routers(app_cfg, router_cfg)
    app_cfg.backend_resolved = mode
    audit = AuditLogger(app_cfg.logging,
                        secrets=[os.environ.get("GEMINI_MCP_TOKEN", "")])
    return Deps(router=router, app_cfg=app_cfg, audit=audit, logger=logger,
                router_optimal=router_optimal, backend=mode)


def _run_stdio(app_cfg: AppCfg, router_cfg) -> int:
    from .server import build_server
    deps = _deps(app_cfg, router_cfg)
    deps.logger.info("gemini-mcp %s starting (stdio, backend=%s)",
                     __version__, deps.backend)
    build_server(deps).run(transport="stdio")
    return 0


def _run_serve(app_cfg: AppCfg, router_cfg, args) -> int:
    import uvicorn

    from .server import build_http_app, build_server
    token = os.environ.get("GEMINI_MCP_TOKEN", "")
    if not token:
        console.print("[red]GEMINI_MCP_TOKEN is required for HTTP mode[/red] "
                      "(generate: openssl rand -hex 32)")
        return 2
    deps = _deps(app_cfg, router_cfg)
    app = build_http_app(build_server(deps), app_cfg, token)
    bind = args.bind or app_cfg.server.http_bind
    port = args.port or app_cfg.server.http_port
    deps.logger.info("gemini-mcp %s serving on http://%s:%d%s",
                     __version__, bind, port, app_cfg.server.mcp_path)
    uvicorn.run(app, host=bind, port=port, log_level="warning", access_log=False)
    return 0


def _run_logs(app_cfg: AppCfg, args) -> int:
    log_dir = Path(app_cfg.logging.dir)
    if args.json:
        entries = AuditLogger(app_cfg.logging).tail(args.n, date=args.date)
        print(json.dumps(entries, indent=1, ensure_ascii=False))
        return 0
    path = log_dir / "app.log"
    if not path.exists():
        console.print(f"[yellow]no {path} yet[/yellow]")
        return 0
    with path.open(encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
        for line in lines[-args.n:]:
            sys.stdout.write(line)
        if not args.follow:
            return 0
        try:
            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(0.4)
        except KeyboardInterrupt:
            return 0


def _run_prune(app_cfg: AppCfg, router_cfg, args) -> int:
    lcfg = app_cfg.logging
    removed = prune_jsonl(lcfg, dry=args.dry)
    router, _, mode = make_routers(app_cfg, router_cfg)
    res = router.ledger.prune(audit_keep_days=args.audit_days or lcfg.audit_keep_days,
                              cache_ttl_s=router_cfg.cache_ttl_h * 3600, dry=args.dry)
    days = (0 if args.dry or mode == "direct"
            else enforce_audit_size(router.ledger, lcfg.audit_max_mb))
    out = {"backend": mode, "jsonl_removed": removed, **res,
           "audit_window_days_now": days or None, "dry": args.dry}
    console.print(json.dumps(out, default=str))
    return 0


_DIRECT_COLD_NOTE = ("direct mode: quota counters live in-memory inside the running "
                     "gateway process; this fresh-process view shows limits, not live "
                     "spend (live: quota_status tool or meta.quota_after)")


def _run_quota(app_cfg: AppCfg, router_cfg, args) -> int:
    if resolve_mode(app_cfg, router_cfg) == "router":
        return cmd_quota(router_cfg, args)
    router, _, _ = make_routers(app_cfg, router_cfg)
    rows = router.quota_table()
    if args.json:
        print(json.dumps({"backend": "direct", "note": _DIRECT_COLD_NOTE, "rows": rows},
                         indent=1, default=str))
        return 0
    console.print(f"[bold cyan]Gemini quota (direct mode)[/bold cyan] — {_DIRECT_COLD_NOTE}")
    for r in rows:
        console.print(f"  {r['key_id']} {r['model']}: rpd {r['rpd_left']}/{r['rpd_limit']}"
                      f" · rpm {r['rpm_used']}/{r['rpm_limit']}"
                      + (f" · wait {r['wait_s']}s ({r['reason']})" if r["wait_s"] else "")
                      + (" · [green]recommended[/green]" if r["recommended"] else ""))
    return 0


def _run_calls(app_cfg: AppCfg, router_cfg, args) -> int:
    if resolve_mode(app_cfg, router_cfg) == "router":
        return cmd_calls(router_cfg, args)
    note = "direct mode keeps no persistent call log (in-process only)"
    if args.json:
        print(json.dumps({"calls": [], "note": note}, indent=1))
        return 0
    console.print(f"[yellow]{note}[/yellow]")
    return 0


def _doctor_direct(app_cfg: AppCfg, router_cfg, args) -> int:
    """doctor without touching disk state (cmd_doctor would create a SQLite ledger)."""
    import asyncio

    from gemini_router.config import key_id
    from gemini_router.ledger import iso, pt_midnight_ts
    ok = True
    console.print("[bold]gemini-router (direct mode — in-memory state)[/bold]")
    if router_cfg.api_keys:
        console.print(f"  keys: {len(router_cfg.api_keys)} → "
                      + ", ".join(key_id(k) for k in router_cfg.api_keys))
    else:
        console.print("  [red]no API keys configured "
                      "(GEMINI_API_KEYS / GEMINI_API_KEY_1..10)[/red]")
        ok = False
    tz_now = pt_midnight_ts(router_cfg.quota_day_tz)
    console.print(f"  quota day ({router_cfg.quota_day_tz}): resets in "
                  f"{(pt_midnight_ts(router_cfg.quota_day_tz, days_ahead=1) - tz_now) / 3600:.1f}"
                  f" h (midnight was {iso(tz_now)[:19]} UTC)")
    console.print(f"  chain: {' → '.join(router_cfg.chain)}")
    console.print(f"  limits/key×model: {router_cfg.limits_per_key_model.model_dump()} · "
                  f"wait: {router_cfg.wait.model_dump()}")
    if not args.offline and router_cfg.api_keys:
        async def _probe() -> None:
            router, _, _ = make_routers(app_cfg, router_cfg)
            try:
                avail = await router.availability()
                for kid, models in avail.items():
                    console.print(f"  {kid}: " + (f"listModels OK ({len(models)} models)"
                                                  if models is not None
                                                  else "[yellow]listModels failed[/yellow]"))
            finally:
                await router.aclose()
        asyncio.run(_probe())
    return 0 if ok else 1


def _run_doctor(app_cfg: AppCfg, router_cfg, args) -> int:
    mode = resolve_mode(app_cfg, router_cfg)
    rc = (_doctor_direct(app_cfg, router_cfg, args) if mode == "direct"
          else cmd_doctor(router_cfg, args))
    console.print(f"[bold]gemini-mcp {__version__}[/bold] (backend={mode})")
    log_dir = Path(app_cfg.logging.dir)
    writable = os.access(log_dir.parent if not log_dir.exists() else log_dir, os.W_OK)
    console.print(f"  logs dir: {log_dir} (writable={writable})")
    console.print(f"  http: {app_cfg.server.http_bind}:{app_cfg.server.http_port}"
                  f"{app_cfg.server.mcp_path} · token set="
                  f"{bool(os.environ.get('GEMINI_MCP_TOKEN'))}")
    console.print(f"  pipeline: {app_cfg.pipeline.model_dump()}")
    console.print(f"  thinking default: {app_cfg.thinking.default}")
    secrets_line = redact("keys look like AIzaSyDUMMYdummy123 in logs", [])
    console.print(f"  redaction self-test: {'OK' if 'AIza' not in secrets_line else 'FAIL'}")
    return rc if rc == 0 and writable else 1


def _project_root() -> Path | None:
    """Project root when running from an installed-layout venv: <root>/.venv/bin/exe."""
    exe = Path(sys.executable or "")
    if len(exe.parents) >= 3 and exe.parents[1].name == ".venv":
        root = exe.parents[2]
        if (root / "config.yaml").exists() or (root / ".env").exists():
            return root
    return None


def _bootstrap_env() -> None:
    """Bare launches (Freebuff → ``ssh root@vps …/gemini-mcp stdio``) start with
    cwd=$HOME and an empty env: the systemd EnvironmentFile does not apply there.
    Anchor cwd to the project root and load its .env (without overriding already-set
    vars) so keys, config.yaml and logs/ behave identically on every transport."""
    if root := _project_root():
        try:
            os.chdir(root)
        except OSError:
            pass
    try:
        from dotenv import load_dotenv
    except ImportError:                      # declared in deps; degrade gracefully
        return
    env_file = os.environ.get("GEMINI_MCP_ENV_FILE")
    for cand in ([Path(env_file)] if env_file else []) + [Path.cwd() / ".env"]:
        if cand.is_file():
            load_dotenv(cand, override=False)
            return


def main(argv: list[str] | None = None) -> int:
    _bootstrap_env()
    ap = argparse.ArgumentParser(prog="gemini-mcp",
                                 description="Gemini free-tier MCP gateway")
    ap.add_argument("--config", help="config.yaml path (or GEMINI_MCP_CONFIG)")
    ap.add_argument("--version", action="version", version=f"gemini-mcp {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stdio", help="run MCP over stdio (Freebuff via SSH)")
    p = sub.add_parser("serve", help="run MCP streamable-HTTP daemon + REST")
    p.add_argument("--bind")
    p.add_argument("--port", type=int)
    p = sub.add_parser("quota", help="budget table (delegates to gemini-router)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("calls", help="audit tail (delegates to gemini-router)")
    p.add_argument("-n", type=int, default=50)
    for flag in ("--model", "--status", "--source", "--request-id"):
        p.add_argument(flag)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("logs", help="tail app log / request JSONL")
    p.add_argument("-n", type=int, default=100)
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--json", action="store_true", help="request JSONL instead of app.log")
    p.add_argument("--date", help="YYYY-MM-DD for --json")
    p = sub.add_parser("prune", help="evict old logs/audit rows (retention)")
    p.add_argument("--audit-days", type=float)
    p.add_argument("--dry", action="store_true")
    p = sub.add_parser("doctor", help="router + mcp health check")
    p.add_argument("--offline", action="store_true")

    args = ap.parse_args(argv)
    try:
        app_cfg, router_cfg = load_all(args.config)
    except Exception as e:  # noqa: BLE001 — config errors must be readable
        console.print(f"[red]config error:[/red] {e}")
        return 2

    try:
        if args.cmd == "stdio":
            return _run_stdio(app_cfg, router_cfg)
        if args.cmd == "serve":
            return _run_serve(app_cfg, router_cfg, args)
        if args.cmd == "quota":
            return _run_quota(app_cfg, router_cfg, args)
        if args.cmd == "calls":
            return _run_calls(app_cfg, router_cfg, args)
        if args.cmd == "logs":
            return _run_logs(app_cfg, args)
        if args.cmd == "prune":
            return _run_prune(app_cfg, router_cfg, args)
        if args.cmd == "doctor":
            return _run_doctor(app_cfg, router_cfg, args)
    except Exception as e:  # noqa: BLE001 — CLI must never traceback at the user
        console.print(f"[red]error:[/red] {redact(str(e))}")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
