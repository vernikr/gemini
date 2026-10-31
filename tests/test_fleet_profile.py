from __future__ import annotations

import json
import stat

import pytest

from gemini_mcp import fleet_profile as profile


def test_profile_is_atomic_private_and_contains_no_credentials(tmp_path, monkeypatch):
    home = tmp_path / "config"
    monkeypatch.setenv("GEMINI_MCP_HOME", str(home))
    path = profile.save_profile("vps.example", "deploy")
    assert path == home / "fleet.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert json.loads(path.read_text()) == {"host": "vps.example", "user": "deploy"}
    assert profile.load_profile() == profile.FleetProfile("vps.example", "deploy")
    assert not list(home.glob(".fleet-*"))


def test_profile_rejects_whitespace_in_connection_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_MCP_HOME", str(tmp_path / "config"))
    with pytest.raises(ValueError, match="whitespace"):
        profile.save_profile("vps.example; command", "deploy")


def test_profile_rejects_corrupt_or_invalid_saved_host(tmp_path, monkeypatch):
    home = tmp_path / "config"
    monkeypatch.setenv("GEMINI_MCP_HOME", str(home))
    home.mkdir()
    (home / "fleet.json").write_text('{"host":"bad host"}')
    with pytest.raises(ValueError, match="invalid host"):
        profile.load_profile()
