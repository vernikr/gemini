"""gemini CLI: serve | stdio | quota | calls | logs | prune | doctor | smoke.
Terminal observability (FR-E7) works over SSH against the shared state without
touching the daemon."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import NoReturn

from gemini_router.cli import cmd_calls, cmd_doctor, cmd_quota
from rich.console import Console

from . import __version__, ingest
from .backends import make_routers, resolve_mode
from .config import AppCfg, load_all
from .logging_setup import AuditLogger, enforce_audit_size, prune_jsonl, redact, setup_logging
from .prompts import BREVITY_RULE
from .tools import Deps

console = Console(stderr=True, highlight=False)   # highlight=False: no auto-underlined
                                                  # URLs in printed commands (confusing:
                                                  # they are not clickable)


def _deps(app_cfg: AppCfg, router_cfg) -> Deps:
    logger = setup_logging(app_cfg.logging,
                           secrets=[os.environ.get("GEMINI_MCP_TOKEN", "")])
    router, router_optimal, mode = make_routers(app_cfg, router_cfg)
    audit = AuditLogger(app_cfg.logging,
                        secrets=[os.environ.get("GEMINI_MCP_TOKEN", "")])
    return Deps(router=router, app_cfg=app_cfg, audit=audit, logger=logger,
                router_optimal=router_optimal, backend=mode)


def _run_stdio(app_cfg: AppCfg, router_cfg) -> int:
    from .server import build_server
    deps = _deps(app_cfg, router_cfg)
    deps.logger.info("gemini %s starting (stdio, backend=%s)",
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
    deps.logger.info("gemini %s serving on http://%s:%d%s",
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
    audit = {} if args.dry else enforce_audit_size(router.ledger, lcfg.audit_max_mb)
    # Third ingest prune trigger (issuance, a finished analyze_file, and this): the
    # on-demand handle for an operator who wants the inbox empty now. None under
    # --dry, which is a projection for the ledger only and must not read as "0".
    icfg = app_cfg.ingest
    inbox = None if args.dry else ingest.prune(Path(icfg.dir), icfg.keep_hours,
                                               icfg.total_cap_mb)
    out = {"backend": mode, "jsonl_removed": removed, **res, **audit,
           "inbox_removed": inbox, "dry": args.dry}
    console.print(json.dumps(out, default=str))
    return 0


def _effective_cfg(app_cfg: AppCfg, router_cfg):
    """The router config for the ledger this mode actually reads. Direct mode keeps its
    own file under the config home, so its read-only commands must be pointed there or
    they report on an empty default database."""
    from .backends import direct_state_path
    if resolve_mode(app_cfg, router_cfg) != "direct":
        return router_cfg
    state = direct_state_path(router_cfg)
    Path(state).parent.mkdir(parents=True, exist_ok=True)
    return router_cfg.model_copy(update={"state_path": state})


def _run_quota(app_cfg: AppCfg, router_cfg, args) -> int:
    return cmd_quota(args, _effective_cfg(app_cfg, router_cfg))


def _run_calls(app_cfg: AppCfg, router_cfg, args) -> int:
    return cmd_calls(args, _effective_cfg(app_cfg, router_cfg))


def _run_smoke(app_cfg: AppCfg, router_cfg, args) -> int:
    """First real-key verification: free checks, then one tiny paid call.

    Budget: doctor and countTokens never spend (kind='count'); exactly one generate
    call, so one RPD on the model the chain picks.
    """
    import asyncio

    from gemini_router import RouterRequest

    rc = cmd_doctor(args, _effective_cfg(app_cfg, router_cfg))
    if rc != 0:
        console.print("[yellow]smoke stopped: doctor failed — fix the above first[/yellow]")
        return rc

    router, _, mode = make_routers(app_cfg, router_cfg)

    async def _free() -> None:
        try:
            print("count_tokens →", await router.count_tokens("Hello world! " * 40))
        finally:
            await router.aclose()

    console.print("\n[bold]countTokens[/bold] (free pre-flight)")
    asyncio.run(_free())

    console.print("\n[bold]one tiny generate[/bold] (spends 1 RPD; thinking low to keep "
                  "it cheap)")
    # No small max_output_tokens: 3.x thinking tokens count toward the cap, so a tiny
    # cap starves the visible answer.
    res = router.complete_sync(RouterRequest(
        prompt="Reply with exactly one word: OK", kind="single", source="smoke",
        thinking_level="low"))
    console.print(f"  model={res.model} key={res.key_id} cached={res.cached} "
                  f"in={res.tokens_in} out={res.tokens_out} thought={res.tokens_thought} "
                  f"finish={res.finish_reason}")
    console.print(f"  text={res.text!r}")

    console.print("\n[bold]quota[/bold] (the model above must show one RPD spent)")
    _run_quota(app_cfg, router_cfg, args)

    ok = res.text.strip().upper().startswith("OK")
    console.print(f"\n[green]SMOKE OK[/green] — backend={mode}" if ok
                  else f"\n[red]SMOKE FAILED[/red] — the model answered "
                       f"{res.text!r}, expected 'OK'")
    return 0 if ok else 1


def _log_dir_writable(log_dir: Path) -> bool:
    """Can logs actually be written here?

    `os.access` answers False for a path that does not exist yet, so checking the
    directory alone reports "logging is broken" on every fresh install — where the
    directory simply has not been created. Walk up to the nearest existing ancestor:
    if that is writable, the log dir can be created on first write.
    """
    for cand in (log_dir, *log_dir.parents):
        if cand.exists():
            return os.access(cand, os.W_OK)
    return False


def _run_doctor(app_cfg: AppCfg, router_cfg, args) -> int:
    rc = cmd_doctor(args, _effective_cfg(app_cfg, router_cfg))
    console.print(f"[bold]gemini {__version__}[/bold] "
                  f"(backend={resolve_mode(app_cfg, router_cfg)})")
    log_dir = Path(app_cfg.logging.dir)
    writable = _log_dir_writable(log_dir)
    console.print(f"  logs dir: {log_dir} (writable={writable})")
    console.print(f"  http: {app_cfg.server.http_bind}:{app_cfg.server.http_port}"
                  f"{app_cfg.server.mcp_path} · token set="
                  f"{bool(os.environ.get('GEMINI_MCP_TOKEN'))}")
    console.print(f"  pipeline: {app_cfg.pipeline.model_dump()}")
    console.print(f"  thinking default: {app_cfg.thinking.default}")
    secrets_line = redact("keys look like AIzaSyDUMMYdummy123 in logs", [])
    console.print(f"  redaction self-test: {'OK' if 'AIza' not in secrets_line else 'FAIL'}")
    if not writable:
        console.print("[red]FAILED[/red] — the log directory is not writable, so "
                      "nothing will be recorded")
        return 6 if rc == 0 else rc
    return rc


def _add_compute_flags(p) -> None:
    """One vocabulary for every compute subcommand — not a dialect per command."""
    p.epilog = "LENGTH (required): " + BREVITY_RULE
    p.formatter_class = argparse.RawDescriptionHelpFormatter
    p.add_argument("--task", help="what to do with the body (required for analyze)")
    p.add_argument("--prompt", help="the prompt (ask). Prefer --prompt-file or stdin: "
                                    "argv is visible in ps/history and length-limited")
    p.add_argument("--prompt-file", help="read the prompt from this file")
    p.add_argument("--body-file", help="read the body from this file (analyze). The "
                                       "body is never taken from argv")
    p.add_argument("--system", help="system instruction (ask only)")
    p.add_argument("--upload-id", help="analyze an already-uploaded file by its id")
    p.add_argument("-f", "--file", action="append", dest="file",
                   help="file to analyze (reserved: the CLI file lane is not wired yet)")
    p.add_argument("--thinking", choices=["low", "medium", "high"],
                   help="thinking level (default: the config default)")
    p.add_argument("--format", choices=["text", "json"], default="text",
                   help="answer format requested from the model")
    p.add_argument("--max-output-tokens", type=int,
                   help="cap per model call. Small values starve the visible answer: "
                        "on thinking models the cap covers thought tokens too")
    p.add_argument("--model", help="pin one model (skips the fallback chain)")
    p.add_argument("--overflow", choices=["off", "lite", "lite+gemma"], default="off",
                   help="weaker tiers when Flash quotas run out (route 'optimal' "
                        "upgrades this to lite+gemma on its own)")
    p.add_argument("--route", choices=["optimal", "best_available"],
                   help="routing profile (default: the config default)")
    p.add_argument("--wait-for-minute-limits", dest="wait_for_minute_limits",
                   action="store_true", default=None,
                   help="wait out RPM/TPM windows (default: wait)")
    p.add_argument("--no-wait", dest="wait_for_minute_limits", action="store_false",
                   help="do not wait out minute windows; route on or fail fast")
    p.add_argument("--request-id", help="idempotency key; repeating it replays the "
                                        "cached result")
    p.add_argument("--fields", help="comma-separated meta fields to return "
                                    "(default: a short set; 'all' for everything)")
    p.add_argument("--json", action="store_true",
                   help="force JSON on stdout (already the default when stdout is "
                        "not a terminal)")


def _project_root() -> Path | None:
    """Project root when running from an installed-layout venv: <root>/.venv/bin/exe."""
    exe = Path(sys.executable or "")
    if len(exe.parents) >= 3 and exe.parents[1].name == ".venv":
        root = exe.parents[2]
        if (root / "config.yaml").exists() or (root / ".env").exists():
            return root
    return None


def _bootstrap_env() -> None:
    """Bare launches (Freebuff → ``ssh root@vps …/gemini stdio``) start with
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
    from .config import config_home
    home_env = config_home() / ".env"
    cands = ([Path(env_file)] if env_file else []) \
        + [home_env, Path.cwd() / ".env"]
    for cand in cands:
        if cand.is_file():
            load_dotenv(cand, override=False)
            if cand == home_env:
                # anchor cwd to the config home (relative logging.dir, config.yaml
                # discovery): bare launches — incl. the uv-tool binary on the VPS,
                # where config-home may symlink to the app dir — behave like the
                # old .venv-layout anchor.
                try:
                    os.chdir(config_home())
                except OSError:
                    pass
            return


def _run_setup(args) -> int:
    from .config import parse_keys
    from .setup_wizard import render_report, run_setup
    keys = args.keys
    if args.keys_file:
        kfp = Path(args.keys_file).expanduser()
        if not kfp.is_file():
            console.print(f"[red]keys file not found:[/red] {kfp}")
            return 2
        merged = parse_keys(keys or "") + parse_keys(kfp.read_text(encoding="utf-8"))
        keys = ",".join(dict.fromkeys(merged)) or None
    rep = run_setup(backend=args.backend, keys=keys, probe=not args.offline,
                    force=args.force, json_out=args.json, console=console,
                    vps_host=args.vps_host, vps_user=args.vps_user,
                    projects_dir=args.projects_dir,
                    write_clients=args.write_clients)
    if args.json:
        print(json.dumps(rep, indent=1, ensure_ascii=False, default=str))
    else:
        render_report(rep, console)
    return 2 if rep.get("error") else 0


def _run_uninstall(args) -> int:
    from .setup_wizard import render_report, run_uninstall
    rep = run_uninstall(yes=args.yes, json_out=args.json)
    if args.json:
        print(json.dumps(rep, indent=1, ensure_ascii=False, default=str))
    else:
        render_report(rep, console)
    return 2 if rep.get("error") else 0


class _FriendlyParser(argparse.ArgumentParser):
    """argparse's failures, made usable: the error line, a `did you mean` when a known
    command or flag is one edit away, and the one command that lists everything. A CLI
    whose vocabulary grows (setup, doctor, quota, keys, prune…) owes the person typing a
    way back from a typo — the alternative is a wall of `choose from …`."""

    def _known_words(self) -> list[str]:
        """Every word the user could have meant: flags, subcommand names and the literal
        choices of positional arguments (`keys list|add|remove`). Harvested from the parser
        tree so a new subcommand is covered the moment it is registered."""
        words: list[str] = []
        for action in self._actions:
            words += list(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    words.append(name)
                    words += sub._known_words() if isinstance(sub, _FriendlyParser) else []
            elif isinstance(action.choices, (list, tuple, dict)):
                # the literal values a positional accepts (`keys list|add|remove`) — a typo
                # there is as common as one in a command name
                words += [c for c in action.choices if isinstance(c, str)]
        return [w for w in words if w not in ("-h", "--help")]

    def error(self, message: str) -> NoReturn:
        import difflib
        import re as _re

        # argparse quotes invalid choices but leaves unrecognized flags bare
        tokens = _re.findall(r"'([^']+)'", message) + _re.findall(r"--[A-Za-z][\w-]*", message)
        known = self._known_words()
        bare = {w.lstrip("-"): w for w in known}
        hint = ""
        for token in tokens:
            close = difflib.get_close_matches(token.lstrip("-"), bare, n=1, cutoff=0.6)
            if close:
                hint = f"did you mean '{bare[close[0]]}'?"
                break
        self.print_usage(sys.stderr)
        sys.stderr.write(f"gemini: error: {message}\n")
        if hint:
            sys.stderr.write(f"  {hint}\n")
        sys.stderr.write("  run 'gemini --help' for the command list, "
                         "'gemini <command> --help' for one command\n")
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    _bootstrap_env()
    ap = _FriendlyParser(
        prog="gemini",
        description="Gemini free-tier MCP gateway — one key pool (or one server), shared "
                    "quota, one ledger.",
        epilog="""common commands:
  gemini setup                 onboarding wizard (keys + client config)
  gemini doctor                keys, chain, limits, live probe — the health check
  gemini quota                 what is left, per key×model
  gemini keys                  the key pool: fingerprints, where it lives
  gemini keys add <key>        add a key: validated, probed free, service restarted
  gemini analyze_file          (MCP tool) read a PDF/image/audio file
""",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config.yaml path (or GEMINI_MCP_CONFIG)")
    ap.add_argument("--keys", help="API key(s), comma-separated (overrides every other "
                                   "source; visible in ps/history — prefer --keys-file)")
    ap.add_argument("--keys-file", help="file with key(s): comma/whitespace/newline "
                                        "separated, # comments ok (or GEMINI_MCP_KEYS_FILE)")
    ap.add_argument("--version", action="version", version=f"gemini {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("setup", help="one-shot onboarding: keys+config in "
                                     "~/.config/gemini, client snippets")
    p.add_argument("--backend", choices=["direct", "router"],
                   help="skip the fleet-vs-simple question")
    p.add_argument("--keys", help="AI Studio keys, comma-separated (non-interactive)")
    p.add_argument("--keys-file", help="read keys from a file (comma/whitespace/newline "
                                       "separated, # comments ok)")
    p.add_argument("--offline", action="store_true", help="skip the live key probe")
    p.add_argument("--force", action="store_true", help="overwrite existing keys/config")
    p.add_argument("--vps-host", help="fleet: VPS host/IP to deploy to (prompted if TTY)")
    p.add_argument("--vps-user", help="fleet: SSH user on the VPS (default root)")
    p.add_argument("--projects-dir", help="fleet: folder for the repo clones "
                                          "(default ~/projects; cloned or updated "
                                          "automatically)")
    p.add_argument("--write-client", action="append", dest="write_clients",
                   choices=["freebuff", "claude_desktop", "claude", "cursor"],
                   help="also MERGE the gemini entry into that client's mcp.json "
                        "(timestamped .bak backup; repeatable — leaves only "
                        "'restart the client' for the human)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("uninstall", help="remove wizard-written keys+config")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--json", action="store_true")
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
    p = sub.add_parser("prune", help="evict old logs/audit rows/inbox blobs (retention)")
    p.add_argument("--audit-days", type=float)
    p.add_argument("--dry", action="store_true")
    p = sub.add_parser("smoke", help="real-key end-to-end check (spends ≤1 RPD)")
    p.add_argument("--offline", action="store_true",
                   help="skip the live key probe in the doctor step")
    p.add_argument("--json", action="store_true", help="print the quota table as JSON")
    p = sub.add_parser("doctor", help="router + mcp health check")
    p.add_argument("--offline", action="store_true")
    p = sub.add_parser("keys", help="manage the API-key pool (writes the daemon's env "
                                    "file, probes new keys free, restarts the service)")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove"])
    p.add_argument("items", nargs="*",
                   help="keys to add, or refs (index / fingerprint prefix / the key) to "
                        "remove")
    p.add_argument("--env-file", help="override the env file to edit (default: the systemd "
                                      "unit's EnvironmentFile, else the wizard config home)")
    p.add_argument("--check", action="store_true",
                   help="list: probe every key live (listModels — free, no RPD)")
    p.add_argument("--offline", action="store_true", help="skip the live probe")
    p.add_argument("--no-restart", action="store_true",
                   help="write the pool but leave the service on its old keys")
    p.add_argument("--force", action="store_true",
                   help="keep a key that failed the probe / remove the last key")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("skill", help="install the agent skill (routing, badges, quota "
                                     "policy) where the loaders look for it")
    sp = p.add_subparsers(dest="skill_action")
    pi = sp.add_parser("install", help="copy the packaged skill into ~/.agents/skills "
                                       "and ~/.claude/skills")
    pi.add_argument("--print", dest="print_only", action="store_true",
                    help="show the plan, change nothing")
    sp.add_parser("status", help="what is installed where, and whether it is stale")
    sp.add_parser("uninstall", help="remove only what this command installed")
    p.set_defaults(skill_action="")            # bare `skill` is a question, not a guess

    p = sub.add_parser("ask", help="single-shot completion (MCP gemini_generate). "
                                   "Prompt from --prompt, --prompt-file or stdin")
    _add_compute_flags(p)
    p = sub.add_parser("analyze", help="chunked analysis of a massive text body "
                                       "(MCP analyze_large). Body from --body-file or stdin")
    _add_compute_flags(p)

    args = ap.parse_args(argv)
    if not args.cmd:                              # bare invocation: show the way in
        ap.print_help()
        print("\nstart here: gemini setup — or gemini doctor if this machine is "
              "already set up")
        return 0
    if args.cmd in ("setup", "uninstall"):       # works even with a broken/absent config
        return _run_setup(args) if args.cmd == "setup" else _run_uninstall(args)
    if args.cmd == "keys":                       # file-level: never needs the app config
        from .keys_cmd import run as run_keys

        return run_keys(args)
    if args.cmd == "skill":                      # file-level: never needs the app config
        from .skill_cmd import run as run_skill

        return run_skill(args)
    try:
        from .config import parse_keys
        over: dict = {}
        arg_keys: list[str] = parse_keys(args.keys or "")
        if args.keys_file:
            kfp = Path(args.keys_file).expanduser()
            if not kfp.is_file():
                console.print(f"[red]keys file not found:[/red] {kfp}")
                return 2
            arg_keys += parse_keys(kfp.read_text(encoding="utf-8"))
        if arg_keys:
            over["api_keys"] = list(dict.fromkeys(arg_keys))
        app_cfg, router_cfg = load_all(args.config, **over)
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
        if args.cmd == "smoke":
            return _run_smoke(app_cfg, router_cfg, args)
        if args.cmd in ("ask", "analyze"):
            from .compute_cmd import run_analyze, run_analyze_file, run_ask

            deps = _deps(app_cfg, router_cfg)
            if args.cmd == "ask":
                return run_ask(deps, app_cfg, args)
            if args.upload_id:      # the file lane's server-side half: already uploaded
                return run_analyze_file(deps, app_cfg, args)
            return run_analyze(deps, app_cfg, args)
    except Exception as e:  # noqa: BLE001 — CLI must never traceback at the user
        console.print(f"[red]error:[/red] {redact(str(e))}")
        if "no API keys" in str(e):
            console.print("[yellow]fix:[/yellow] run [bold]gemini setup[/bold] — or "
                          "provide keys via --keys / --keys-file / "
                          "GEMINI_MCP_KEYS_FILE / GEMINI_API_KEYS "
                          "(e.g. in ~/.config/gemini/.env)")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
