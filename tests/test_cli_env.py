"""v0.2.1: bare-launch bootstrap — ssh stdio starts with cwd=$HOME and no env."""
from __future__ import annotations

import os
from pathlib import Path

from gemini_mcp import cli


def test_load_dotenv_no_override(monkeypatch, tmp_path):
    f = tmp_path / ".env"
    f.write_text("GEMINI_TEST_NEW=fromfile\nGEMINI_TEST_SET=fromfile\n")
    monkeypatch.setenv("GEMINI_MCP_ENV_FILE", str(f))
    monkeypatch.setenv("GEMINI_TEST_SET", "prior")
    monkeypatch.delenv("GEMINI_TEST_NEW", raising=False)
    cli._bootstrap_env()
    assert os.environ["GEMINI_TEST_NEW"] == "fromfile"
    assert os.environ["GEMINI_TEST_SET"] == "prior"        # override=False
    monkeypatch.delenv("GEMINI_TEST_NEW", raising=False)


def test_anchor_cwd_to_project_root(monkeypatch, tmp_path):
    root = tmp_path / "proj"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / "config.yaml").write_text("{}\n")
    exe = root / ".venv" / "bin" / "python"
    exe.touch()
    monkeypatch.setattr(cli.sys, "executable", str(exe))
    monkeypatch.chdir(tmp_path)
    cli._bootstrap_env()
    assert Path.cwd() == root                              # config.yaml/logs anchor


def test_no_anchor_outside_venv_layout(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.sys, "executable", str(tmp_path / "bin" / "python3"))
    monkeypatch.chdir(tmp_path)
    cli._bootstrap_env()
    assert Path.cwd() == tmp_path                          # unchanged
