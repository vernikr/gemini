"""The config dir was renamed with the project; the keys inside it were not moved."""
from pathlib import Path

from gemini_mcp.config import config_home


def test_config_home_prefers_a_pre_rename_dir_with_keys(tmp_path, monkeypatch):
    """An upgrade must not read as 'no keys configured': the ledger and .env live in the
    old directory, and nothing in this change moves them."""
    monkeypatch.delenv("GEMINI_MCP_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    legacy = tmp_path / ".config" / "gemini-mcp"
    legacy.mkdir(parents=True)

    assert config_home() == legacy


def test_config_home_uses_the_new_dir_on_a_fresh_install(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_MCP_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert config_home() == tmp_path / ".config" / "gemini"


def test_config_home_new_dir_wins_once_it_exists(tmp_path, monkeypatch):
    """After the user moves across, the legacy dir must stop shadowing the new one."""
    monkeypatch.delenv("GEMINI_MCP_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".config" / "gemini-mcp").mkdir(parents=True)
    (tmp_path / ".config" / "gemini").mkdir(parents=True)

    assert config_home() == tmp_path / ".config" / "gemini"


def test_config_home_env_override_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_MCP_HOME", str(tmp_path / "elsewhere"))

    assert config_home() == tmp_path / "elsewhere"
