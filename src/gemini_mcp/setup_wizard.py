"""`gemini setup` — idempotent consumer onboarding.

Writes $GEMINI_MCP_HOME (~/.config/gemini)/{config.yaml,.env 0600,fleet.json}, optionally
probes keys live (listModels on an in-memory ledger — zero disk state, zero RPD spend),
prints ready-to-paste client snippets. Non-interactive with --json/--keys/--backend;
interactive otherwise (one fleet-vs-simple question, masked key paste). Re-run = no-op
+ report unless --force. Secrets never appear in output — only sha256 fingerprints."""
from __future__ import annotations

import asyncio
import getpass
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import yaml
from gemini_router import Router
from gemini_router.config import Config as RouterConfig
from gemini_router.config import key_id

from . import __version__
from .config import config_home
from .fleet_profile import profile_path, save_profile

FLEET_REPOS = ("gemini", "gemini-router")     # github.com/vernikr/<name>, private
FLEET_HEALTHZ = "curl -fsS --max-time 3 http://127.0.0.1:8790/healthz"

# Same JSON shape everywhere: {"mcpServers": {"gemini": {…}}} — safe to merge.
CLIENT_CONFIGS = {
    "freebuff": "~/.agents/mcp.json",
    "claude_desktop": "~/Library/Application Support/Claude/claude_desktop_config.json",
    "cursor": "~/.cursor/mcp.json"}
CLIENT_ALIASES = {"claude": "claude_desktop"}
VERIFY_PROMPT = ('Use the quota_status tool from the "gemini" MCP server and show '
                 'me the current quotas.')


class SetupError(RuntimeError):
    """Readable one-line failure for the fleet auto-deploy (rendered, never traceback)."""


def _primary_entry(snippets: dict) -> dict:
    """The mcpServers entry a client should get: fleet = ssh, direct = npx if present
    (pnpm-first product) else version-pinned uvx."""
    if snippets.get("ssh"):
        return snippets["ssh"]
    return snippets["pnpm_npx"] if shutil.which("npx") else snippets["uvx_pinned"]


def _write_client_configs(names: list[str], entry: dict, deployed_fleet: bool,
                          report: dict) -> None:
    """Merge the `gemini` entry into each client's mcp.json (backup first). Every
    failure is a structured {client, error, fix} — an agent can act on it without
    reading prose."""
    for raw in names:
        name = CLIENT_ALIASES.get(raw, raw)
        rel = CLIENT_CONFIGS.get(name)
        if not rel:
            report.setdefault("clients_failed", []).append(
                {"client": raw, "error": "unknown client",
                 "fix": f"--write-client accepts: {', '.join(CLIENT_CONFIGS)}"})
            continue
        path = Path(rel).expanduser()
        if not deployed_fleet and entry.get("command") == "ssh" \
                and "<" in " ".join(entry.get("args", [])):
            report.setdefault("clients_failed", []).append(
                {"client": name, "path": str(path),
                 "error": "server not deployed yet — the snippet still has a "
                          "<YOUR_VPS> placeholder",
                 "fix": "finish the deploy first (deploy_steps above), then re-run "
                        "with --write-client"})
            continue
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
                if not isinstance(data, dict) or not isinstance(
                        data.get("mcpServers", {}), dict):
                    raise ValueError("not an {\"mcpServers\": {…}} object")
            except Exception as e:  # noqa: BLE001 — never clobber a broken config
                report.setdefault("clients_failed", []).append(
                    {"client": name, "path": str(path),
                     "error": f"existing config is unusable ({e})",
                     "fix": f"repair the JSON in {path} (or remove it), then re-run; "
                            "or paste the block manually"})
                continue
            backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            backup.write_bytes(path.read_bytes())
        else:
            backup = None
        data.setdefault("mcpServers", {})["gemini"] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        report.setdefault("clients_written", []).append(
            {"client": name, "path": str(path),
             "backup": str(backup) if backup else None})

ENV_HEADER = ("# gemini credentials — written by `gemini setup` (mode 0600).\n"
              "# Keys: https://aistudio.google.com/app/apikey (free tier).\n")
CFG_HEADER = "# gemini app config — written by `gemini setup`.\n"


def _tool_bin(user: str) -> str:
    home = "/root" if user == "root" else f"/home/{user}"
    return f"{home}/.local/bin/gemini"      # uv-tool binary — the CD-managed one


def _client_snippets(backend: str, fleet: dict | None = None,
                     deployed: bool = False) -> dict:
    """Structured wiring guidance. Direct = paste NOW (renderer picks npx/uvx by PATH).
    Fleet: after the wizard's own auto-deploy succeeded it is ALSO paste-now (with the
    real host baked in); otherwise fall back to short manual steps (no repo-doc refs —
    dlx users don't have the repo)."""
    paste_where = dict(CLIENT_CONFIGS)
    if backend == "direct":
        return {"paste_now": True,
                "pnpm_npx": {"command": "npx",
                             "args": ["-y", "@vernikr/gemini", "stdio"]},
                "uvx_pinned": {"command": "uvx",
                               "args": ["--from", f"vernikr-gemini=={__version__}",
                                        "gemini", "stdio"]},
                "paste_where": paste_where}
    host = (fleet or {}).get("host") or "<YOUR_VPS>"
    user = (fleet or {}).get("user") or "root"
    ssh_entry = {"command": "ssh",
                 "args": [f"{user}@{host}", _tool_bin(user), "stdio"]}
    if deployed:
        return {"paste_now": True, "ssh": ssh_entry, "paste_where": paste_where}
    return {"paste_now": False, "ssh": ssh_entry,
            "deploy_steps": [
                f"re-run with the wizard doing the work: gemini setup --backend "
                f"router --vps-host {host}   (clones/updates the repos, provisions "
                f"the server, pushes keys — needs git+ssh auth from this machine, "
                f"macOS/Linux)",
                "or fully manual: git clone https://github.com/vernikr/gemini and "
                "…/gemini-router side by side (folder already exists? "
                "git -C <dir> pull --ff-only instead of cloning)",
                f"from inside the gemini clone: VPS_HOST={host} VPS_USER={user} "
                "bash scripts/run_remote.sh setup   (streams its own progress)",
                f"keys: ssh {user}@{host} and set GEMINI_API_KEYS=<key>,<key> in "
                "/opt/apps/gemini-mcp/.env (chmod 600), then "
                "systemctl restart gemini",
                "THEN paste the ssh snippet below into your client config and "
                "restart the client"]}


def _short_path(p: str | Path) -> str:
    """Normalize a user-home path without displaying machine-specific details."""
    s = str(Path(p).expanduser())
    home = str(Path.home())
    return "~" + s[len(home):] if s == home or s.startswith(home + os.sep) else s


def _ask_keys(console, *, purpose: str, default: str) -> str | None:
    """Key intake as one visible three-way choice. If the terminal cannot safely
    provide hidden input, fall back to the file option rather than raising."""
    from rich.prompt import Prompt
    if console:
        console.print(f"Get Gemini API keys for {purpose}:")
        console.print("  1) paste now (hidden input)  ·  2) read from a file  ·  "
                      "3) skip")
    choice = Prompt.ask("Keys", choices=["1", "2", "3"], default=default,
                        console=console)
    if choice == "1":
        try:
            val = getpass.getpass("keys (comma-separated, hidden): ").strip()
            return val or None
        except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 — tty may refuse
            if console:
                console.print(f"[yellow]hidden input failed ({type(e).__name__}) — "
                              "some terminals crash on echo-off; re-run and pick "
                              "2 (file), or pass --keys/--keys-file[/yellow]")
            return None
    if choice == "2":
        from .config import parse_keys
        for _ in range(3):
            p = Prompt.ask("Path to keys file (comma/whitespace/newline separated, "
                           "# comments ok; empty = skip)", default="", console=console)
            if not p.strip():
                return None
            fp = Path(p.strip()).expanduser()
            if not fp.is_file():
                if console:
                    console.print(f"[red]not a file:[/red] {fp}")
                continue
            ks = parse_keys(fp.read_text(encoding="utf-8"))
            if ks:
                return ",".join(ks)
            if console:
                console.print("[yellow]no keys found in that file[/yellow]")
    return None


def _ver_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for p in str(v).split(".")[:3]:
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def _sync_repos(projects_dir: Path, log: list[str]) -> Path:
    """Clone-or-update both fleet repos under projects_dir; return the mcp repo path.
    Existing clones are pulled (never re-cloned: 'destination path already exists'
    can't happen); a non-git non-empty dir is a readable error."""
    if not shutil.which("git"):
        raise SetupError("git not found on PATH — install git, or deploy manually")
    projects_dir.mkdir(parents=True, exist_ok=True)
    for name in FLEET_REPOS:
        d = projects_dir / name
        if (d / ".git").is_dir():
            r = subprocess.run(["git", "-C", str(d), "pull", "--ff-only", "--quiet"],
                               capture_output=True, text=True, timeout=300)
            log.append(f"updated {d}" if r.returncode == 0 else
                       f"kept {d} as-is (git pull failed: "
                       f"{(r.stderr or r.stdout).strip()[:120]})")
        elif d.exists() and any(d.iterdir()):
            raise SetupError(f"{d} exists but is not a git clone — move it aside "
                             "and re-run")
        else:
            r = subprocess.run(["git", "clone", "--quiet",
                                f"https://github.com/vernikr/{name}.git", str(d)],
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                raise SetupError(f"git clone {name} failed: "
                                 f"{(r.stderr or r.stdout).strip()[:200]} — the repos "
                                 "are private; your git must authenticate to GitHub "
                                 "(gh auth login or a credential helper)")
            log.append(f"cloned {d}")
    return projects_dir / "gemini"


def _ssh(host: str, user: str, remote: str, *, input: str | None = None,
         timeout: int = 30, batch: bool = True):
    cmd = ["ssh"] + (["-o", "BatchMode=yes"] if batch else []) \
        + ["-o", "ConnectTimeout=10", f"{user}@{host}", remote]
    return subprocess.run(cmd, input=input, capture_output=True, text=True,
                          timeout=timeout)


def _server_health(host: str, user: str) -> dict | None:
    try:
        r = _ssh(host, user, FLEET_HEALTHZ, timeout=25)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except Exception:                                  # noqa: BLE001 — probe is best-effort
        return None


def _provision(repo: Path, host: str, user: str) -> None:
    if sys.platform == "win32":
        raise SetupError("fleet auto-deploy needs macOS/Linux (bash+rsync+ssh) — "
                         "use the manual steps below")
    script = repo / "scripts" / "run_remote.sh"
    if not script.is_file():
        raise SetupError(f"{script} missing in the clone — cannot provision")
    env = {**os.environ, "VPS_HOST": host, "VPS_USER": user}
    r = subprocess.run(["bash", str(script), "setup"], cwd=str(repo), env=env)
    if r.returncode != 0:
        raise SetupError(f"provisioning failed (run_remote.sh rc={r.returncode}; "
                         "its output is above) — fix: verify ssh works "
                         f"(`ssh {user}@{host} 'echo ok'`, key auth), rsync is "
                         "installed on both ends, then re-run the same command")


def _push_keys(host: str, user: str, keys_csv: str) -> None:
    """Keys travel over STDIN — never in argv (ps/history on both ends stay clean)."""
    remote = ("read -r k; f=/opt/apps/gemini-mcp/.env; "
              "[ -f $f ] || { echo 'server .env missing (provision first)' >&2; exit 1; }; "
              "sed -i '/^GEMINI_API_KEYS=/d' $f; echo \"GEMINI_API_KEYS=$k\" >> $f; "
              "chmod 600 $f; systemctl restart gemini-mcp.service")
    r = _ssh(host, user, remote, input=keys_csv + "\n", timeout=40, batch=False)
    if r.returncode != 0:
        raise SetupError("could not write keys to the server .env (ssh rc="
                         f"{r.returncode}: {(r.stderr or '').strip()[:160]}) — fix: "
                         "provisioning must have run once (.env is created there); "
                         f"check `ssh {user}@{host} 'ls -la /opt/apps/gemini-mcp/.env'`")


def deploy_fleet(*, host: str, user: str, projects_dir: Path,
                 keys_csv: str | None) -> dict:
    """End-to-end fleet deploy: repos → provision (skipped when the server already
    runs this version or newer) → keys → health. Raises SetupError with context."""
    log: list[str] = []
    repo = _sync_repos(projects_dir, log)
    pre = _server_health(host, user)
    if pre and pre.get("ok") and _ver_tuple(pre.get("version", "")) >= _ver_tuple(__version__):
        log.append(f"server already runs gemini {pre.get('version')} "
                   f"(>= {__version__}) — provisioning skipped")
    else:
        log.append(f"provisioning {user}@{host} via scripts/run_remote.sh setup "
                   "(output streams below)")
        _provision(repo, host, user)
        log.append("provisioning finished")
    if keys_csv:
        _push_keys(host, user, keys_csv)
        log.append("API keys written to the server .env (0600); unit restarted")
    health = None
    for _ in range(20):
        health = _server_health(host, user)
        if health and health.get("ok"):
            break
        time.sleep(1.5)
    if not health or not health.get("ok"):
        raise SetupError(f"server not healthy after deploy — check: ssh {user}@{host} "
                         "systemctl status gemini")
    log.append(f"healthz ok: version={health.get('version')} "
               f"backend={health.get('backend')}")
    return {"log": log, "healthz": health}


async def _probe_keys(keys: list[str]) -> list[dict]:
    # ':memory:' → the real Ledger on a throwaway SQLite db: no files, no RPD spend
    router = Router(RouterConfig.model_validate({"api_keys": keys,
                                                 "state_path": ":memory:"}))
    out = []
    try:
        # per-key on purpose: this is the one place where "does THIS key work" is the
        # question, and routing's shared probe cannot answer it.
        avail = await router.availability_per_key()
        for k in keys:
            models = avail.get(key_id(k))
            out.append({"key_id": key_id(k),
                        "ok": models is not None,
                        "models": len(models) if models else None})
    finally:
        await router.aclose()
    return out


def run_setup(*, backend: str | None = None, keys: str | None = None,
              probe: bool = True, force: bool = False, json_out: bool = False,
              console=None, vps_host: str | None = None, vps_user: str | None = None,
              projects_dir: str | None = None,
              write_clients: list[str] | None = None) -> dict:
    """Interactive when neither --backend/--keys nor --json pins the flow. Fleet mode
    auto-deploys the server side (clone/pull repos → run_remote.sh → keys → health)
    and finishes with a paste-NOW snippet carrying the real host. Returns the report
    dict (secret-free: key fingerprints only)."""
    home = config_home()
    interactive = not json_out and keys is None and backend is None
    if interactive:
        from rich.prompt import Prompt
        if console:
            console.print("[bold]gemini setup[/bold]")
            console.print("  1) [bold]Simple[/bold] — direct to Google AI Studio from "
                          "this machine (no server; default)")
            console.print("  2) [bold]Fleet[/bold] — gateway on my VPS/router "
                          "(shared quota ledger; the wizard deploys it for you)")
        choice = Prompt.ask("How will you run gemini?", choices=["1", "2"],
                            default="1", console=console)
        backend = "direct" if choice == "1" else "router"
    backend = backend or "direct"
    report: dict = {"tool": "setup", "backend": backend, "config_home": str(home),
                    "created": [], "kept": [], "overwritten": [], "key_ids": []}
    fleet: dict = {}
    if backend == "router":
        from rich.prompt import Prompt
        tty = sys.stdin.isatty() and not json_out
        fleet = {"host": vps_host or os.environ.get("VPS_HOST") or "",
                 "user": vps_user or os.environ.get("VPS_USER") or "root",
                 "projects_dir": str(Path(
                     projects_dir or os.environ.get("GEMINI_MCP_PROJECTS_DIR")
                     or Path.home() / "projects").expanduser())}
        if tty and not fleet["host"]:
            fleet["host"] = Prompt.ask("VPS host/IP for the fleet gateway",
                                       console=console)
            fleet["user"] = Prompt.ask("SSH user on that server",
                                       default=fleet["user"], console=console)
            fleet["projects_dir"] = str(Path(Prompt.ask(
                "Projects folder (repos are cloned/updated there, nothing is "
                "downloaded twice; ~ ok)",
                default=_short_path(fleet["projects_dir"]),
                console=console)).expanduser())
        if tty and keys is None:
            keys = _ask_keys(console, purpose="the SERVER (it keeps them; this "
                                              "machine stores none)", default="3")
        if keys:
            fleet["key_ids"] = [key_id(k.strip()) for k in keys.split(",") if k.strip()]
        report["fleet"] = fleet
    if (backend == "direct" and keys is None and json_out
            and not os.environ.get("GEMINI_API_KEYS")):
        report["error"] = ("keys required in --json mode: pass --keys '<key>', "
                           "--keys-file <path>, or set GEMINI_API_KEYS")
        report["ok"] = False
        return report
    if keys is None and not json_out and backend == "direct":
        if console:
            console.print("[dim]free-tier keys: aistudio.google.com/app/apikey[/dim]")
        keys = _ask_keys(console, purpose="this machine (direct mode)", default="1")

    home.mkdir(parents=True, exist_ok=True)
    env_path, cfg_path = home / ".env", home / "config.yaml"
    if backend == "router" and fleet.get("host"):
        profile = profile_path()
        existed = profile.exists()
        save_profile(fleet["host"], fleet["user"])
        report["overwritten" if existed else "created"].append(str(profile))

    key_list = [k.strip() for k in (keys or os.environ.get("GEMINI_API_KEYS", "")
                                    ).split(",") if k.strip()]
    if backend == "direct":
        if not key_list:
            report["error"] = ("no API keys provided — re-run and paste at least one "
                               "key (AI Studio or Cloud Console; both work)")
            report["ok"] = False
            return report
        report["key_ids"] = [key_id(k) for k in key_list]
        line = "GEMINI_API_KEYS=" + ",".join(key_list) + "\n"
        existed = env_path.exists()
        if existed and not force:
            report["kept"].append(f"{env_path} (existing keys kept; --force to replace)")
            report.pop("key_ids")     # report reflects on-disk truth, not the new paste
        else:
            env_path.write_text(ENV_HEADER + line, encoding="utf-8")
            env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)          # 0600: keys at rest
            report["overwritten" if existed else "created"].append(str(env_path))
    # logs live beside the config so stdio launches (cwd=$HOME) never litter ~/logs
    cfg_body = {"backend": backend, "logging": {"dir": str(home / "logs")}}
    if cfg_path.exists() and not force:
        report["kept"].append(str(cfg_path))
    else:
        existed = cfg_path.exists()
        cfg_path.write_text(CFG_HEADER + yaml.safe_dump(cfg_body, sort_keys=False),
                            encoding="utf-8")
        report["overwritten" if existed else "created"].append(str(cfg_path))

    if probe and key_list and backend == "direct":   # fleet keys live (and probe) on
        try:                                          # the server — never spent here
            report["probe"] = asyncio.run(_probe_keys(key_list))
        except Exception as e:  # noqa: BLE001 — offline must not fail the setup
            report["probe"] = {"error": str(e)[:200],
                               "hint": "keys are saved; re-check with `gemini doctor`"}
    deployed = False
    if backend == "router":
        keys_csv = ",".join(k.strip() for k in (keys or "").split(",")
                            if k.strip()) or None
        if not fleet.get("host"):
            report["deploy_error"] = ("no VPS host — re-run interactively (it will "
                                      "ask) or pass --vps-host <ip>")
        elif not shutil.which("ssh"):
            report["deploy_error"] = "ssh client not found on PATH — deploy manually"
        else:
            if console:
                console.print(f"[bold]→ deploying the fleet gateway to "
                              f"{fleet['user']}@{fleet['host']} …[/bold]")
            try:
                report["deploy"] = deploy_fleet(
                    host=fleet["host"], user=fleet["user"],
                    projects_dir=Path(fleet["projects_dir"]), keys_csv=keys_csv)
                deployed = True
            except SetupError as e:
                report["deploy_error"] = str(e)
            except Exception as e:  # noqa: BLE001 — wizard must stay readable
                report["deploy_error"] = f"{type(e).__name__}: {e}"[:300]
    report["snippets"] = _client_snippets(backend, fleet=fleet or None,
                                          deployed=deployed)
    if write_clients:
        _write_client_configs(write_clients, _primary_entry(report["snippets"]),
                              deployed_fleet=(backend == "router" and deployed),
                              report=report)
    written = report.get("clients_written") or []
    if backend == "router" and not deployed:
        steps = ["finish the server deploy with the steps above, then RE-RUN this "
                 "same command — it verifies the server and writes the client "
                 "config itself with --write-client",
                 "paste the final ssh block into your MCP client config and "
                 "restart the client fully",
                 'verify — send your agent the prompt below (MCP server "gemini", '
                 "tool quota_status)"]
    else:
        if written:
            steps = ["client config written: " + "; ".join(
                f"{w['client']} → {w['path']}"
                + (f" (backup: {w['backup']})" if w.get("backup") else "")
                for w in written)]
        else:
            steps = ["paste the JSON block above into your MCP client config "
                     "(file paths are listed under the block)"]
        steps.append("restart the client fully (configs are read at startup)"
                     + ("; the first gemini call takes ~10–20 s — one-time dependency "
                        "resolve" if backend == "direct" else ""))
        steps.append("verify — send your agent the prompt below; the quota_status "
                     'tool lives in the MCP server named "gemini"')
    report["steps"] = steps
    report["verify_prompt"] = VERIFY_PROMPT
    report["ok"] = not (report.get("error") or report.get("deploy_error")
                        or report.get("clients_failed"))
    return report


def run_uninstall(*, yes: bool = False, json_out: bool = False) -> dict:
    home = config_home()
    targets = [home / ".env", home / "config.yaml", home / "fleet.json", home / "state.db",
               home / "state.db-wal", home / "state.db-shm"]
    present = [p for p in targets if p.exists()]
    report: dict = {"tool": "uninstall", "config_home": str(home),
                    "removed": [str(p) for p in present], "kept": []}
    if not present:
        report["note"] = "nothing to remove"
        return report
    if not yes:
        return {"tool": "uninstall", "error": "confirm_required",
                "config_home": str(home),
                "would_remove": [str(p) for p in present],
                "hint": "re-run with --yes to delete keys+config"}
    for p in present:
        p.unlink()
    if home.exists() and not any(home.iterdir()):
        home.rmdir()
    return report


def render_report(rep: dict, console) -> None:
    """Human view of the setup/uninstall report (rich), secrets already stripped."""
    if rep.get("error"):
        console.print(f"[red]{rep['error']}[/red]")
        if rep.get("hint"):
            console.print(f"  hint: {rep['hint']}")
        return
    for p in rep.get("created", []):
        console.print(f"  [green]+ created[/green] {p}")
    for p in rep.get("overwritten", []):
        console.print(f"  [yellow]~ overwritten[/yellow] {p}")
    for p in rep.get("kept", []):
        console.print(f"  = kept {p}")
    for p in rep.get("removed", []):
        console.print(f"  [red]- removed[/red] {p}")
    if rep.get("note"):
        console.print(f"  {rep['note']}")
    if rep.get("key_ids"):
        console.print(f"  keys: {', '.join(rep['key_ids'])} (fingerprints)")
    for row in rep.get("probe", []) if isinstance(rep.get("probe"), list) else []:
        mark = "[green]ok[/green]" if row["ok"] else "[red]listModels FAILED[/red]"
        console.print(f"  probe {row['key_id']}: {mark}"
                      + (f", {row['models']} models" if row.get("models") else ""))
    if isinstance(rep.get("probe"), dict):
        console.print(f"  [yellow]probe skipped: {rep['probe'].get('error')}[/yellow]")
    for line in (rep.get("deploy") or {}).get("log", []):
        console.print(f"  [green]✓[/green] {line}")
    if (rep.get("fleet") or {}).get("key_ids"):
        console.print(f"  keys pushed to the SERVER: "
                      f"{', '.join(rep['fleet']['key_ids'])} (fingerprints)")
    if rep.get("deploy_error"):
        console.print(f"  [yellow]deploy: {rep['deploy_error']}[/yellow]")
    for w in rep.get("clients_written") or []:
        console.print(f"  [green]✓ wrote[/green] {w['client']}: {w['path']}"
                      + (f" [dim](backup: {w['backup']})[/dim]" if w.get("backup") else ""))
    for f in rep.get("clients_failed") or []:
        console.print(f"  [red]✗ {f['client']}:[/red] {f['error']}")
        console.print(f"    [yellow]fix:[/yellow] {f['fix']}")
    snip = rep.get("snippets") or {}
    if snip.get("paste_now"):
        entry = _primary_entry(snip)
        written = rep.get("clients_written") or []
        if written:
            console.print("[bold green]▶ Client config written — this block went "
                          "in:[/bold green]")
        else:
            console.print("[bold green]▶ The whole wiring is this one block:[/bold green]")
        console.print(json.dumps({"mcpServers": {"gemini": entry}}, indent=1))
        if not written:
            for name, path in (snip.get("paste_where") or {}).items():
                console.print(f"    [dim]{name}:[/dim] {path}")
        if snip.get("pnpm_npx") and entry == snip.get("pnpm_npx"):
            console.print(f"  [dim]equivalent alternative form: "
                          f"{json.dumps(snip['uvx_pinned'], ensure_ascii=False)}[/dim]")
    elif snip.get("ssh"):
        console.print("[bold yellow]▶ Server not deployed yet — do NOT paste into "
                      "your client.[/bold yellow] Fastest path:")
        for i, step in enumerate(snip.get("deploy_steps", []), 1):
            console.print(f"  {i}. {step}")
        console.print("  [bold]then[/bold] paste:")
        console.print(json.dumps({"mcpServers": {"gemini": snip["ssh"]}}, indent=1))
    steps = rep.get("steps") or []
    if steps:
        console.print("[bold]Do this now:[/bold]")
        for i, s in enumerate(steps, 1):
            console.print(f"  [bold]{i}.[/bold] {s}")
        if rep.get("verify_prompt"):
            console.print(f"     [bold cyan]“{rep['verify_prompt']}”[/bold cyan]")
