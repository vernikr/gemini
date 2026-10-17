"""Key sources (0.9.0): parse_keys format, keys-file plumbing (env + config.yaml +
CLI global flags + setup), and the precedence chain CLI > keys-file > yaml > env."""
from __future__ import annotations

import json

import pytest

from gemini_mcp import cli
from gemini_mcp.cli import main
from gemini_mcp.config import load_all, parse_keys

K1, K2, K3 = "AIzaSyONE_fake_1", "AIzaSyTWO_fake_2", "AIzaSyTHREE_fake3"


def test_parse_keys_formats():
    assert parse_keys(f"{K1},{K2}") == [K1, K2]
    assert parse_keys(f"{K1}\n{K2}  {K3}\n") == [K1, K2, K3]
    assert parse_keys(f"# my keys\n{K1}, {K1}\n{K2}  # trailing note\n") == [K1, K2]
    assert parse_keys("") == []


def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_MCP_HOME", str(tmp_path / "gmcp"))
    monkeypatch.delenv("GEMINI_MCP_CONFIG", raising=False)
    monkeypatch.delenv("GEMINI_ROUTER_CONFIG", raising=False)
    monkeypatch.delenv("GEMINI_MCP_KEYS_FILE", raising=False)
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    # keep _bootstrap_env from anchoring cwd to the repo root (tests run inside the
    # project's .venv layout, which the anchor heuristic would treat as installed)
    monkeypatch.setattr(cli.sys, "executable", str(tmp_path / "bin" / "python3"))
    monkeypatch.chdir(tmp_path)
    return tmp_path / "gmcp"


def test_load_all_keys_file_env(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    kf = tmp_path / "keys.txt"
    kf.write_text(f"# pool\n{K1},{K2}\n")
    monkeypatch.setenv("GEMINI_MCP_KEYS_FILE", str(kf))
    _, rc = load_all(None)
    assert rc.api_keys == [K1, K2]


def test_load_all_keys_file_in_yaml_and_missing_file(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    kf = tmp_path / "keys.txt"
    kf.write_text(K3)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"router:\n  keys_file: {kf}\n")
    _, rc = load_all(None)
    assert rc.api_keys == [K3]
    cfg.unlink()                                       # yaml keys_file wins while present
    monkeypatch.setenv("GEMINI_MCP_KEYS_FILE", str(tmp_path / "nope.txt"))
    with pytest.raises(FileNotFoundError):
        load_all(None)


def test_keys_precedence_cli_over_file_over_env(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    kf = tmp_path / "keys.txt"
    kf.write_text(K2)
    monkeypatch.setenv("GEMINI_MCP_KEYS_FILE", str(kf))
    monkeypatch.setenv("GEMINI_API_KEYS", K3)          # lowest priority
    _, rc = load_all(None)
    assert rc.api_keys == [K2]                          # file beats env
    _, rc = load_all(None, api_keys=[K1])
    assert rc.api_keys == [K1]                          # explicit CLI beats file


def test_cli_global_keys_file_feeds_quota(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    kf = tmp_path / "keys.txt"
    kf.write_text(f"{K1}\n{K2}\n")
    rc = main(["--keys-file", str(kf), "quota", "--json"])
    rows = json.loads(capsys.readouterr().out)          # same shape in both modes
    assert rc == 0 and rows
    kids = {r["key_id"] for r in rows}
    assert len(kids) == 2                               # both keys in the budget table


def test_cli_global_keys_missing_file_rc2(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    rc = main(["--keys-file", str(tmp_path / "nope.txt"), "quota", "--json"])
    assert rc == 2 and "keys file not found" in capsys.readouterr().err


def test_setup_from_keys_file(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path, monkeypatch)
    kf = tmp_path / "keys.txt"
    kf.write_text(f"# pool\n{K1}, {K2}\n")
    rc = main(["setup", "--backend", "direct", "--keys-file", str(kf),
               "--offline", "--json"])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0 and rep["key_ids"] and K1 not in rep and K2 not in str(rep)
    env = (home / ".env").read_text()
    assert K1 in env and K2 in env                      # stored, fingerprints printed
def test_doctor_exit_codes_pass_through(tmp_path, monkeypatch):
    """The router's codes name the failure; collapsing them to 1 threw the reason away."""
    _home(tmp_path, monkeypatch)
    monkeypatch.setenv("GEMINI_MCP_BACKEND", "direct")
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    assert main(["doctor", "--offline"]) == 3          # no keys

    monkeypatch.setenv("GEMINI_API_KEYS", "AIzaSyDUMMYdummy1234567890ab")
    assert main(["doctor", "--offline"]) == 0

    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"logging:\n  dir: {ro}/app.log\n")
    rc = main(["--config", str(cfg), "doctor", "--offline"])
    ro.chmod(0o755)                                    # let tmp_path clean up
    assert rc == 6                                     # log dir unwritable


def test_doctor_does_not_cry_wolf_on_a_fresh_install(tmp_path, monkeypatch, capsys):
    """A first run has no logs/ directory yet — that is not a failure. The old check
    called os.access on a path that did not exist, got False, and reported logging as
    broken with exit 6, so `gemini doctor` failed on every clean machine."""
    _home(tmp_path, monkeypatch)
    monkeypatch.setenv("GEMINI_MCP_BACKEND", "direct")
    monkeypatch.setenv("GEMINI_API_KEYS", "AIzaSyDUMMYdummy1234567890ab")

    log_dir = tmp_path / "gmcp" / "logs"
    assert not log_dir.exists(), "the premise is a directory that is not there yet"

    assert main(["doctor", "--offline"]) == 0
    assert "writable=True" in capsys.readouterr().err
