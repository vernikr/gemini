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
    assert rep["snippets"]["generic_mcp_json"]["args"][:2] == [
        "--from", f"gemini-mcp-gateway=={__version__}"]


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
    rep = run_setup(backend="router", json_out=True)
    assert "backend: router" in (home / "config.yaml").read_text()
    assert not (home / ".env").exists()
    assert rep["snippets"]["freebuff_agents_mcp_json"]["command"] == "ssh"


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
    guard = run_uninstall(yes=False)
    assert guard["error"] == "confirm_required" and len(guard["would_remove"]) == 2
    assert (home / ".env").exists()
    rep = run_uninstall(yes=True)
    assert len(rep["removed"]) == 2 and not home.exists()   # empty dir cleaned up
    assert run_uninstall(yes=True)["note"] == "nothing to remove"


def test_load_all_picks_config_home(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)                 # no cwd config.yaml
    run_setup(backend="direct", keys=K1, probe=False, json_out=True)
    app, _ = load_all(None)
    assert app.backend == "direct"


def test_bootstrap_env_reads_config_home_dotenv(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_MCP_ENV_FILE", raising=False)
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    run_setup(backend="direct", keys=K1, probe=False, json_out=True)
    from gemini_mcp.cli import _bootstrap_env
    try:
        _bootstrap_env()
        assert os.environ.get("GEMINI_API_KEYS") == K1
    finally:
        os.environ.pop("GEMINI_API_KEYS", None)


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
