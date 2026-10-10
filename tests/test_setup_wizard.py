"""M7.2 setup wizard: idempotent writes, 0600 secrets, masked reports, config-home
autodiscovery (load_all + _bootstrap_env), uninstall guard, CLI wiring, live probe."""
from __future__ import annotations

import json
import os
import stat

import httpx
import respx
from gemini_router.config import key_id

from gemini_mcp import __version__
from gemini_mcp.cli import main
from gemini_mcp.config import load_all
from gemini_mcp.setup_wizard import run_setup, run_uninstall

BASE = "https://generativelanguage.googleapis.com/v1beta"
K1, K2 = "AIzaSyFAKEkeyONE12345", "AIzaSyFAKEkeyTWO67890"


def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_MCP_HOME", str(tmp_path / "gmcp"))
    monkeypatch.delenv("GEMINI_MCP_CONFIG", raising=False)
    return tmp_path / "gmcp"


def test_setup_direct_writes_env_0600_and_config(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    rep = run_setup(backend="direct", keys=f"{K1}, {K2}", probe=False, json_out=True)
    env, cfg = home / ".env", home / "config.yaml"
    assert env.exists() and cfg.exists()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert K1 in env.read_text() and K2 in env.read_text()
    assert "backend: direct" in cfg.read_text()
    assert rep["key_ids"] == [key_id(K1), key_id(K2)]
    assert set(rep["created"]) >= {str(env), str(cfg)}
    blob = json.dumps(rep)                      # report is secret-free
    assert K1 not in blob and K2 not in blob
    sn = rep["snippets"]
    assert sn["paste_now"] is True
    assert sn["uvx_pinned"]["args"][:2] == ["--from", f"gemini-mcp-gateway=={__version__}"]
    assert sn["pnpm_npx"] == {"command": "npx",
                              "args": ["-y", "@vernikr/gemini-mcp", "stdio"]}
    assert "claude_desktop" in sn["paste_where"]


def test_setup_idempotent_then_force(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    run_setup(backend="direct", keys=K1, probe=False, json_out=True)
    rep2 = run_setup(backend="direct", keys=K2, probe=False, json_out=True)
    assert any("existing keys kept" in k for k in rep2["kept"])
    assert "key_ids" not in rep2 or not rep2["key_ids"]      # on-disk truth wins
    assert K1 in (home / ".env").read_text()
    rep3 = run_setup(backend="direct", keys=K2, probe=False, force=True, json_out=True)
    assert rep3["overwritten"] and K2 in (home / ".env").read_text()
    assert K1 not in (home / ".env").read_text()


def test_setup_json_requires_keys(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    rep = run_setup(json_out=True)
    assert rep["error"].startswith("keys required")


def test_setup_router_mode_no_env(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    monkeypatch.delenv("VPS_HOST", raising=False)
    rep = run_setup(backend="router", json_out=True)
    assert "backend: router" in (home / "config.yaml").read_text()
    assert not (home / ".env").exists()
    sn = rep["snippets"]
    assert sn["paste_now"] is False and sn["ssh"]["command"] == "ssh"
    assert sn["deploy_steps"] and "run_remote.sh" in " ".join(sn["deploy_steps"])
    # terminal guidance must be self-contained: no repo-doc paths a dlx user can't open
    assert "docs/" not in json.dumps(rep)


# ---- fleet auto-deploy (0.10.0): fake subprocess, no real git/ssh in unit tests ----

def _fake_sub(monkeypatch, health=None):
    """Patch setup_wizard.subprocess.run/which/sleep; return the recorded call list.
    `health` = queue of healthz responses (None = unreachable); last one repeats."""
    import types
    from pathlib import Path

    from gemini_mcp import setup_wizard as sw
    calls: list = []
    queue = list(health or [])
    last = {"v": None}

    def fake_run(cmd, **kw):
        calls.append((list(cmd), kw))
        ok = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "clone":
            d = Path(cmd[-1])                     # make the clone "exist"
            (d / ".git").mkdir(parents=True, exist_ok=True)
            (d / "scripts").mkdir(exist_ok=True)
            (d / "scripts" / "run_remote.sh").write_text("#!/bin/bash\n")
            return ok
        if cmd[0] == "ssh" and "healthz" in cmd[-1]:
            h = queue.pop(0) if queue else last["v"]
            last["v"] = h
            if h is None:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="down")
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(h), stderr="")
        return ok

    monkeypatch.setattr(sw.subprocess, "run", fake_run)
    monkeypatch.setattr(sw.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    return calls


def _ok_health():
    return {"ok": True, "version": __version__, "backend": "router"}


def test_fleet_auto_deploy_provisions_and_pastes(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    calls = _fake_sub(monkeypatch, health=[None, _ok_health()])   # down → provisioned
    rep = run_setup(backend="router", vps_host="YOUR_VPS_HOST",
                    projects_dir=str(tmp_path / "projects"), probe=False, json_out=True)
    assert not rep.get("deploy_error")
    assert rep["deploy"]["healthz"]["version"] == __version__
    clones = [c for c, _ in calls if c[0] == "git" and c[1] == "clone"]
    assert len(clones) == 2                            # both repos, folders were new
    prov = [(c, kw) for c, kw in calls
            if c[0] == "bash" and any("run_remote.sh" in part for part in c)]
    assert prov and prov[0][1]["env"]["VPS_HOST"] == "YOUR_VPS_HOST" \
        and prov[0][1]["env"]["VPS_USER"] == "root"
    sn = rep["snippets"]
    assert sn["paste_now"] is True                     # deployed ⇒ paste NOW
    assert sn["ssh"]["args"][0] == "root@YOUR_VPS_HOST"      # real host baked in
    assert sn["ssh"]["args"][1] == "/root/.local/bin/gemini-mcp"
    assert not (tmp_path / "gmcp" / ".env").exists()   # no keys at rest locally


def test_fleet_skips_provision_when_server_current(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    proj = tmp_path / "projects"
    for name in ("gemini-mcp", "gemini-router"):       # existing clones → pull, no
        (proj / name / ".git").mkdir(parents=True)     # 'already exists' fatal (note 3)
    calls = _fake_sub(monkeypatch, health=[_ok_health()])
    rep = run_setup(backend="router", vps_host="YOUR_VPS_HOST", projects_dir=str(proj),
                    probe=False, json_out=True)
    assert not [c for c, _ in calls if c[0] == "bash"]               # no provisioner
    assert not [c for c, _ in calls if c[0] == "git" and c[1] == "clone"]
    assert [c for c, _ in calls if c[0] == "git" and c[1] == "-C"]   # pull happened
    assert any("skipped" in line for line in rep["deploy"]["log"])
    assert rep["snippets"]["paste_now"] is True


def test_fleet_existing_non_git_dir_readable_error(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    proj = tmp_path / "projects"
    (proj / "gemini-mcp").mkdir(parents=True)
    (proj / "gemini-mcp" / "junk.txt").write_text("x")
    _fake_sub(monkeypatch, health=[])
    rep = run_setup(backend="router", vps_host="YOUR_VPS_HOST", projects_dir=str(proj),
                    probe=False, json_out=True)
    assert "move it aside" in rep["deploy_error"]
    assert rep["snippets"]["paste_now"] is False       # falls back to manual steps


def test_fleet_keys_go_via_stdin_not_argv(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    calls = _fake_sub(monkeypatch, health=[_ok_health()])
    rep = run_setup(backend="router", vps_host="YOUR_VPS_HOST", keys=K1,
                    projects_dir=str(tmp_path / "projects"), probe=False, json_out=True)
    push = [(c, kw) for c, kw in calls if c[0] == "ssh" and "GEMINI_API_KEYS" in c[-1]]
    assert push and push[0][1]["input"].startswith(K1)  # secret travels via stdin
    assert all(K1 not in " ".join(c) for c, _ in calls)  # never in argv (ps-clean)
    assert K1 not in json.dumps(rep)                     # report is secret-free
    assert rep["fleet"]["key_ids"] == [key_id(K1)]


def test_fleet_json_without_host_falls_back(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    monkeypatch.delenv("VPS_HOST", raising=False)
    _fake_sub(monkeypatch, health=[])
    rep = run_setup(backend="router", json_out=True)
    assert "--vps-host" in rep["deploy_error"]
    assert rep["snippets"]["paste_now"] is False
    assert rep["snippets"]["deploy_steps"]


def test_setup_probe_via_respx(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with respx.mock(assert_all_called=False) as rx:
        rx.get(f"{BASE}/models").mock(return_value=httpx.Response(200, json={
            "models": [{"name": "models/gemini-3.8-flash"}]}))
        rep = run_setup(backend="direct", keys=K1, probe=True, json_out=True)
    assert rep["probe"] == [{"key_id": key_id(K1), "ok": True, "models": 1}]


def test_uninstall_guard_and_removal(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    run_setup(backend="direct", keys=K1, probe=False, json_out=True)
    (home / "state.db").write_bytes(b"sqlite")            # local ledger (0.8.1)
    guard = run_uninstall(yes=False)
    assert guard["error"] == "confirm_required" and len(guard["would_remove"]) == 3
    assert (home / ".env").exists()
    rep = run_uninstall(yes=True)
    assert len(rep["removed"]) == 3 and not home.exists()  # empty dir cleaned up
    assert run_uninstall(yes=True)["note"] == "nothing to remove"


def test_load_all_picks_config_home(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)                 # no cwd config.yaml
    run_setup(backend="direct", keys=K1, probe=False, json_out=True)
    app, _ = load_all(None)
    assert app.backend == "direct"


def test_bare_launch_logs_default_to_config_home(tmp_path, monkeypatch):
    """JSON-only wiring (no `setup`, no config.yaml anywhere): logs must not land in
    the arbitrary spawn cwd (clients use $HOME) — they belong beside state.db."""
    home = _home(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    app, _ = load_all(None)
    assert app.logging.dir == str(home / "logs")
    assert app.backend == "auto"                # resolve_mode → direct (no state)


def test_bootstrap_env_reads_config_home_dotenv(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_MCP_ENV_FILE", raising=False)
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    run_setup(backend="direct", keys=K1, probe=False, json_out=True)
    from gemini_mcp.cli import _bootstrap_env
    try:
        _bootstrap_env()
        assert os.environ.get("GEMINI_API_KEYS") == K1
        from pathlib import Path
        assert Path.cwd().resolve() == home.resolve()   # cwd anchored to config home
    finally:                                               # (relative logs/config work
        os.environ.pop("GEMINI_API_KEYS", None)            #  on bare ssh launches)


def test_cli_setup_and_uninstall_json(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    rc = main(["setup", "--backend", "direct", "--keys", K1, "--offline", "--json"])
    out = capsys.readouterr().out
    assert rc == 0 and json.loads(out)["backend"] == "direct"
    assert K1 not in out
    rc = main(["uninstall", "--json"])            # no --yes → guarded
    assert rc == 2 and json.loads(capsys.readouterr().out)["error"] == "confirm_required"
    rc = main(["uninstall", "--yes", "--json"])
    assert rc == 0 and len(json.loads(capsys.readouterr().out)["removed"]) == 2
    os.environ.pop("GEMINI_API_KEYS", None)  # _bootstrap_env loaded setup's .env


def test_render_report_mode_guidance(tmp_path, monkeypatch):
    """The terminal must say WHEN to paste: direct = now, fleet = after deploy."""
    import io

    from rich.console import Console

    from gemini_mcp.setup_wizard import render_report
    _home(tmp_path, monkeypatch)
    monkeypatch.delenv("VPS_HOST", raising=False)
    rep_d = run_setup(backend="direct", keys=K1, probe=False, json_out=True)
    buf = io.StringIO()
    render_report(rep_d, Console(file=buf, force_terminal=False, width=100))
    txt = buf.getvalue()
    assert "Paste this into your MCP client config NOW" in txt
    assert "mcpServers" in txt and ".agents/mcp.json" in txt

    # fleet, auto-deploy SUCCEEDED → same green paste-now block, real host inside
    _fake_sub(monkeypatch, health=[_ok_health()])
    rep_ok = run_setup(backend="router", vps_host="YOUR_VPS_HOST", force=True,
                       projects_dir=str(tmp_path / "p2"), probe=False, json_out=True)
    buf = io.StringIO()
    render_report(rep_ok, Console(file=buf, force_terminal=False, width=100))
    txt = buf.getvalue()
    assert "Paste this into your MCP client config NOW" in txt and "root@YOUR_VPS_HOST" in txt
    assert "Do NOT paste" not in txt and "<YOUR_VPS>" not in txt

    # fleet, no host → yellow manual fallback (numbered, self-contained)
    rep_f = run_setup(backend="router", json_out=True, force=True)
    buf = io.StringIO()
    render_report(rep_f, Console(file=buf, force_terminal=False, width=100))
    txt = buf.getvalue()
    assert "do NOT paste" in txt and "run_remote.sh" in txt
    assert "1." in txt and "<YOUR_VPS>" in txt


def test_bare_launch_without_keys_points_to_setup(tmp_path, monkeypatch, capsys):
    """Fail-fast on missing keys must name the fix command, not just the cause."""
    _home(tmp_path, monkeypatch)
    rc = main(["quota", "--json"])
    err = capsys.readouterr().err
    assert rc == 2 and "no API keys" in err and "gemini-mcp setup" in err


def test_short_path_tilde(tmp_path, monkeypatch):
    from gemini_mcp.setup_wizard import _short_path
    monkeypatch.setenv("HOME", str(tmp_path))          # Path.home() follows HOME
    assert _short_path(tmp_path / "projects") == "~/projects"
    assert _short_path("/opt/other") == "/opt/other"


def test_ask_keys_file_skip_and_crash_guard(tmp_path, monkeypatch):
    """Key intake: file option needs NO tty echo toggle; hidden paste is guarded
    (macOS Terminal.app 2.14 SIGSEGVs when a program turns echo off)."""
    from gemini_mcp import setup_wizard as sw
    kf = tmp_path / "keys.txt"
    kf.write_text(f"# comment\n{K1}\n{K2}\n")

    def feed(answers):
        it = iter(answers)
        monkeypatch.setattr("rich.prompt.Prompt.ask",
                            staticmethod(lambda *a, **k: next(it)))

    feed(["2", str(kf)])                               # file → parsed, deduped
    assert sw._ask_keys(None, purpose="x", default="1") == f"{K1},{K2}"
    feed(["3"])                                        # skip → None
    assert sw._ask_keys(None, purpose="x", default="1") is None
    feed(["2", str(tmp_path / "nope.txt"), ""])        # bad path → retry → empty=skip
    assert sw._ask_keys(None, purpose="x", default="1") is None
    feed(["1"])                                        # hidden paste crashes the tty
    def boom(prompt):
        raise OSError("tty died")
    monkeypatch.setattr(sw.getpass, "getpass", boom)
    assert sw._ask_keys(None, purpose="x", default="1") is None   # no traceback
