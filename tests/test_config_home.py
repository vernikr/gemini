"""Configuration-home selection and environment override behavior."""
from pathlib import Path

from gemini_mcp.config import config_home


def test_config_home_uses_alternate_dir_when_canonical_is_absent(tmp_path, monkeypatch):
    """Use the alternate config directory only when the canonical one is absent."""
    monkeypatch.delenv("GEMINI_MCP_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    alternate = tmp_path / ".config" / "gemini-mcp"
    alternate.mkdir(parents=True)

    assert config_home() == alternate


def test_config_home_uses_the_new_dir_on_a_fresh_install(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_MCP_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert config_home() == tmp_path / ".config" / "gemini"


def test_config_home_new_dir_wins_once_it_exists(tmp_path, monkeypatch):
    """The canonical config directory takes precedence when both exist."""
    monkeypatch.delenv("GEMINI_MCP_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".config" / "gemini-mcp").mkdir(parents=True)
    (tmp_path / ".config" / "gemini").mkdir(parents=True)

    assert config_home() == tmp_path / ".config" / "gemini"


def test_config_home_env_override_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_MCP_HOME", str(tmp_path / "elsewhere"))

    assert config_home() == tmp_path / "elsewhere"
