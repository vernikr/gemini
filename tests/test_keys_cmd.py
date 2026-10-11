"""`gemini-mcp keys`: the pool file is edited atomically, secrets never print, a dead key is
rejected before it is written, and the last key is guarded.

All offline: probes, systemctl and healthz are monkeypatched. The rules pinned here are the
ones a hand-edit gets wrong: partial writes, a 0644 file, duplicates, printing a key into
the terminal scrollback, and an empty pool after a careless remove.
"""
from __future__ import annotations

import json
import stat

import pytest

from gemini_mcp import keys_cmd
from gemini_mcp.cli import main

K1 = "AIzaSyDUMMYfake_pool_key_for_tests_0"
K2 = "AQ.DUMMYfake_spare_key_for_tests_00000000000"


@pytest.fixture()
def pool(tmp_path, monkeypatch):
    """An env file with one key plus unrelated lines, and no systemd anywhere."""
    env = tmp_path / ".env"
    env.write_text("GEMINI_ROUTER_STATE=/tmp/state.db\n"
                   f"GEMINI_API_KEYS={K1}\n"
                   "GEMINI_MCP_TOKEN=deadbeef\n")
    env.chmod(0o600)
    monkeypatch.setattr(keys_cmd.shutil, "which", lambda _name: None)
    monkeypatch.setattr(keys_cmd, "probe", lambda key, base=None: "ok")
    monkeypatch.setattr(keys_cmd, "wait_healthz", lambda timeout_s=10.0: "ok")
    return env


def run(argv: list[str]) -> int:
    return main(argv)


def env_text(path) -> str:
    return path.read_text()


# --------------------------------------------------------------------------- add

def test_add_appends_preserving_other_lines_and_order(pool):
    rc = run(["keys", "add", K2, "--env-file", str(pool), "--no-restart", "--json"])
    assert rc == 0
    body = env_text(pool)
    assert body.splitlines()[1] == f"GEMINI_API_KEYS={K1},{K2}"
    assert "GEMINI_ROUTER_STATE=/tmp/state.db" in body and "GEMINI_MCP_TOKEN=deadbeef" in body


def test_probe_reports_the_transport_the_daemon_will_use(monkeypatch):
    """`ok (query)` is a measured statement, not decoration: keyauth remembered that
    transport for this key, and the daemon's generateContent starts there too."""
    monkeypatch.setattr(keys_cmd.keyauth, "probe_key", lambda key, base=None, **kw: {
        "ok": True, "transport": "query", "status": 200, "reason": "ok"})
    assert keys_cmd.probe(K2) == "ok (query)"
    monkeypatch.setattr(keys_cmd.keyauth, "probe_key", lambda key, base=None, **kw: {
        "ok": True, "transport": "header", "status": 200, "reason": "ok"})
    assert keys_cmd.probe(K1) == "ok"          # the common path stays quiet


def test_probe_surfaces_a_leaked_key_as_itself(monkeypatch):
    monkeypatch.setattr(keys_cmd.keyauth, "probe_key", lambda key, base=None, **kw: {
        "ok": False, "transport": "header", "status": 403,
        "reason": "reported as leaked — delete the key in the console, then add a fresh one"})
    line = keys_cmd.probe(K1)
    assert "leaked" in line and "fresh" in line


def test_list_check_treats_a_rotated_transport_as_healthy(pool, monkeypatch, capsys):
    """The exit code reads the verdict, not the extra word: a key that needed another
    transport is healthy, and a script must see 0."""
    monkeypatch.setattr(keys_cmd, "probe", lambda key, base=None: "ok (query)")
    rc = run(["keys", "list", "--check", "--env-file", str(pool)])
    assert rc == 0 and "ok (query)" in capsys.readouterr().out


def test_add_accepts_a_key_that_needed_another_transport(pool, monkeypatch):
    monkeypatch.setattr(keys_cmd, "probe", lambda key, base=None: "ok (query)")
    rc = run(["keys", "add", K2, "--env-file", str(pool), "--no-restart", "--json"])
    assert rc == 0 and K2 in env_text(pool)


def test_list_check_points_at_the_removal_command(pool, monkeypatch, capsys):
    """The owner added a key, saw two dead ones still listed, and asked whether a removal
    command exists. It did — the listing just never said so."""
    monkeypatch.setattr(keys_cmd, "probe", lambda key, base=None: "rejected (HTTP 401)")
    rc = run(["keys", "list", "--check", "--env-file", str(pool)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "1 rejected — remove: gemini-mcp keys remove" in out


def test_list_check_json_reports_the_rejected_ones(pool, monkeypatch, capsys):
    monkeypatch.setattr(keys_cmd, "probe", lambda key, base=None: "rejected (HTTP 401)")
    run(["keys", "list", "--check", "--env-file", str(pool), "--json"])
    body = json.loads(capsys.readouterr().out)
    assert body["rejected"] == [keys_cmd.fingerprint(K1)]
    assert body["keys"][0]["probe"].startswith("rejected")


def test_plain_list_teaches_check_and_remove(pool, capsys):
    rc = run(["keys", "list", "--env-file", str(pool)])
    out = capsys.readouterr().out
    assert rc == 0 and "--check" in out and "keys remove" in out


def test_add_is_idempotent_and_never_duplicates(pool):
    run(["keys", "add", K2, "--env-file", str(pool), "--no-restart"])
    rc = run(["keys", "add", K1, K2, "--env-file", str(pool), "--no-restart", "--json"])
    assert rc == 0
    assert env_text(pool).count(K2) == 1
    assert len(keys_cmd.read_keys(pool)) == 2


def test_add_creates_the_line_when_missing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OTHER=1\n")
    monkeypatch.setattr(keys_cmd.shutil, "which", lambda _n: None)
    monkeypatch.setattr(keys_cmd, "probe", lambda key, base=None: "ok")
    rc = run(["keys", "add", K1, "--env-file", str(env), "--no-restart"])
    assert rc == 0 and env_text(env).splitlines()[-1] == f"GEMINI_API_KEYS={K1}"


def test_a_loosely_permissioned_file_is_tightened_by_the_next_write(pool):
    pool.chmod(0o644)  # a hand edit that skipped chmod
    run(["keys", "add", K2, "--env-file", str(pool), "--no-restart"])
    assert stat.S_IMODE(pool.stat().st_mode) == 0o600


def test_invalid_key_is_rejected_and_nothing_is_written(pool, capsys):
    before = env_text(pool)
    rc = run(["keys", "add", "has space and is long enough", "--env-file", str(pool),
              "--no-restart"])
    assert rc == 2 and env_text(pool) == before
    assert "not a key-like string" in capsys.readouterr().err


def test_dead_key_is_refused_unless_forced(pool, monkeypatch):
    monkeypatch.setattr(keys_cmd, "probe", lambda key, base=None: "rejected (HTTP 400)")
    rc = run(["keys", "add", K2, "--env-file", str(pool), "--no-restart"])
    assert rc == 2 and len(keys_cmd.read_keys(pool)) == 1
    rc = run(["keys", "add", K2, "--env-file", str(pool), "--no-restart", "--force"])
    assert rc == 0 and len(keys_cmd.read_keys(pool)) == 2


# ------------------------------------------------------------------------ remove

def test_remove_by_index_and_fingerprint(pool):
    run(["keys", "add", K2, "--env-file", str(pool), "--no-restart"])
    fp = keys_cmd.fingerprint(K1)
    rc = run(["keys", "remove", fp[:6], "--env-file", str(pool), "--no-restart"])
    assert rc == 0 and keys_cmd.read_keys(pool) == [K2]
    # the pool never empties by accident: the last key needs --force
    assert run(["keys", "remove", "1", "--env-file", str(pool), "--no-restart"]) == 2
    rc = run(["keys", "remove", "1", "--env-file", str(pool), "--no-restart", "--force"])
    assert rc == 0 and keys_cmd.read_keys(pool) == []


def test_last_key_is_guarded(pool, capsys):
    rc = run(["keys", "remove", "1", "--env-file", str(pool), "--no-restart"])
    assert rc == 2 and keys_cmd.read_keys(pool) == [K1]
    assert "only key" in capsys.readouterr().err


def test_unknown_ref_is_an_error(pool):
    assert run(["keys", "remove", "zzzzzz", "--env-file", str(pool), "--no-restart"]) == 2


# ------------------------------------------------------------------ list / restart

def test_list_shows_fingerprints_but_never_a_raw_key(pool, capsys):
    rc = run(["keys", "list", "--env-file", str(pool)])
    out = capsys.readouterr().out
    assert rc == 0 and keys_cmd.fingerprint(K1) in out and K1 not in out


def test_list_check_flags_a_dead_key(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text(f"GEMINI_API_KEYS={K1}\n")
    monkeypatch.setattr(keys_cmd.shutil, "which", lambda _n: None)
    monkeypatch.setattr(keys_cmd, "probe", lambda key, base=None: "unreachable (boom)")
    assert run(["keys", "list", "--check", "--env-file", str(env)]) == 1
    assert "unreachable" in capsys.readouterr().out


def test_restart_is_called_when_available_and_skippable(pool, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(keys_cmd.shutil, "which", lambda _n: "/usr/bin/systemctl")
    monkeypatch.setattr(keys_cmd, "restart_service",
                        lambda: calls.append(("restart",)) or "restarted")
    run(["keys", "add", K2, "--env-file", str(pool)])
    assert calls == [("restart",)]
    calls.clear()
    run(["keys", "add", "--env-file", str(pool), "--no-restart"])  # nothing to add
    assert calls == []


def test_json_report_shape(pool, capsys):
    rc = run(["keys", "list", "--env-file", str(pool), "--json"])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0 and rep["ok"] and rep["env_file"] == str(pool)
    assert rep["keys"][0]["fingerprint"] == keys_cmd.fingerprint(K1)


# ------------------------------------------------------------- file discovery rules

def test_service_env_file_parses_the_unit_output(monkeypatch):
    class R:
        returncode = 0
        stdout = "/opt/apps/gemini-mcp/.env (ignore_errors=no)\n"

    monkeypatch.setattr(keys_cmd.shutil, "which", lambda _n: "/usr/bin/systemctl")
    monkeypatch.setattr(keys_cmd, "_systemctl", lambda *a: R())
    assert str(keys_cmd.service_env_file()) == "/opt/apps/gemini-mcp/.env"


def test_env_file_falls_back_to_config_home(monkeypatch, tmp_path):
    monkeypatch.setattr(keys_cmd.shutil, "which", lambda _n: None)
    monkeypatch.setenv("GEMINI_MCP_HOME", str(tmp_path / "home"))
    path, source = keys_cmd.resolve_env_file(None)
    assert path == tmp_path / "home" / ".env" and "config home" in source
