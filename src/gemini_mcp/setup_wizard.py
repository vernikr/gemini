"""`gemini-mcp setup` — idempotent consumer onboarding (M7.2, 0.8.0).

Writes $GEMINI_MCP_HOME (~/.config/gemini-mcp)/{config.yaml,.env 0600}, optionally
probes keys live (listModels via MemLedger router — zero disk state, zero RPD spend),
prints ready-to-paste client snippets. Non-interactive with --json/--keys/--backend;
interactive otherwise (one fleet-vs-simple question, masked key paste). Re-run = no-op
+ report unless --force. Secrets never appear in output — only sha256 fingerprints."""
from __future__ import annotations

import asyncio
import getpass
import json
import os
import stat

import yaml
from gemini_router import Router
from gemini_router.config import Config as RouterConfig
from gemini_router.config import key_id

from . import __version__
from .backends import MemLedger
from .config import config_home

ENV_HEADER = ("# gemini-mcp credentials — written by `gemini-mcp setup` (mode 0600).\n"
              "# Keys: https://aistudio.google.com/app/apikey (free tier).\n")
CFG_HEADER = "# gemini-mcp app config — written by `gemini-mcp setup`.\n"


def _client_snippets(backend: str) -> dict[str, dict]:
    core = {"command": "uvx",
            "args": ["--from", f"gemini-mcp-gateway=={__version__}", "gemini-mcp", "stdio"]}
    if backend == "direct":
        return {"generic_mcp_json": core,
                "claude_desktop": core,
                "cursor": core,
                "freebuff_agents_mcp_json": core}
    return {"freebuff_agents_mcp_json": {
        "command": "ssh",
        "args": ["root@<YOUR_VPS>", "/opt/apps/gemini-mcp/.venv/bin/gemini-mcp", "stdio"]},
        "note": ("fleet mode runs on your server; see docs/04-plan.md M4 deploy + "
                 "docs/freebuff-wiring.md for the HTTP-tunnel variant")}


async def _probe_keys(keys: list[str]) -> list[dict]:
    cfg = RouterConfig.model_validate({"api_keys": keys, "state_path": ":memory:"})
    router = Router(cfg, ledger=MemLedger())
    out = []
    try:
        avail = await router.availability()
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
              console=None) -> dict:
    """Interactive when neither --backend/--keys nor --json pins the flow. Returns the
    report dict (secret-free: key fingerprints only)."""
    home = config_home()
    interactive = not json_out and keys is None and backend is None
    if interactive:
        from rich.prompt import Prompt
        if console:
            console.print("[bold]gemini-mcp setup[/bold]")
            console.print("  1) [bold]Simple[/bold] — direct to Google AI Studio from "
                          "this machine (no server; default)")
            console.print("  2) [bold]Fleet[/bold] — gateway on my VPS/router "
                          "(shared quota ledger)")
        choice = Prompt.ask("How will you run gemini-mcp?", choices=["1", "2"],
                            default="1", console=console)
        backend = "direct" if choice == "1" else "router"
    backend = backend or "direct"
    report: dict = {"tool": "setup", "backend": backend, "config_home": str(home),
                    "created": [], "kept": [], "overwritten": [], "key_ids": []}
    if (backend == "direct" and keys is None and json_out
            and not os.environ.get("GEMINI_API_KEYS")):
        report["error"] = ("keys required in --json mode: pass --keys 'AIza…' or set "
                           "GEMINI_API_KEYS")
        return report
    if keys is None and not json_out and backend == "direct":
        if console:
            console.print("Paste your Google AI Studio API key(s), comma-separated "
                          "(input hidden; free tier: aistudio.google.com/app/apikey)")
        keys = getpass.getpass("keys: ")

    home.mkdir(parents=True, exist_ok=True)
    env_path, cfg_path = home / ".env", home / "config.yaml"

    key_list = [k.strip() for k in (keys or os.environ.get("GEMINI_API_KEYS", "")
                                    ).split(",") if k.strip()]
    if backend == "direct":
        if not key_list:
            report["error"] = ("no API keys provided — re-run and paste at least one "
                               "AI Studio key (or --keys 'AIza…')")
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

    if probe and key_list:
        try:
            report["probe"] = asyncio.run(_probe_keys(key_list))
        except Exception as e:  # noqa: BLE001 — offline must not fail the setup
            report["probe"] = {"error": str(e)[:200],
                               "hint": "keys are saved; re-check with `gemini-mcp doctor`"}
    report["snippets"] = _client_snippets(backend)
    report["next"] = ("paste a snippet into your MCP client config, then ask the agent "
                      "to call quota_status" if backend == "direct" else
                      "deploy the fleet side first (docs/04-plan.md M4), then paste the "
                      "ssh snippet")
    return report


def run_uninstall(*, yes: bool = False, json_out: bool = False) -> dict:
    home = config_home()
    targets = [home / ".env", home / "config.yaml", home / "state.db",
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
    snip = (rep.get("snippets") or {}).get("generic_mcp_json") \
        or (rep.get("snippets") or {}).get("freebuff_agents_mcp_json")
    if snip:
        console.print("[bold]client snippet[/bold] (mcpServers entry):")
        console.print(json.dumps({"gemini": snip}, indent=1))
    if rep.get("next"):
        console.print(f"[dim]next: {rep['next']}[/dim]")
