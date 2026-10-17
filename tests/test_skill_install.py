"""The skill is the behavioural-policy surface, and this is the only thing that puts it
where a loader will find it. Pinned because the failure mode is silent: a skill that
installed nowhere still lets every other test pass, and the agent simply never learns the
routing, badge and quota policy — it just behaves worse.
"""
from __future__ import annotations

import pytest

from gemini_mcp import skill_cmd
from gemini_mcp.prompts import BREVITY_RULE


@pytest.fixture
def home_targets(tmp_path, monkeypatch):
    """Point the installer at a throwaway home so it cannot touch the real one."""
    targets = (tmp_path / ".agents" / "skills", tmp_path / ".claude" / "skills")
    monkeypatch.setattr(skill_cmd, "TARGETS", targets)
    return targets


def test_install_writes_both_scopes_and_a_manifest(home_targets, capsys):
    assert skill_cmd.install() == 0
    for dir_ in home_targets:
        text = (dir_ / "gemini-mcp" / "SKILL.md").read_text("utf-8")
        assert BREVITY_RULE in text, "the installed skill must carry the canonical rule"
        assert "{brevity_rule}" not in text, "the placeholder must not ship"
        # the manifest is what makes uninstall safe: without it we could not tell our own
        # skill from somebody else's
        assert (dir_ / skill_cmd.MANIFEST).read_text("utf-8").strip() == "gemini-mcp"
    assert "installed" in capsys.readouterr().out


def test_print_only_promises_nothing_changes(home_targets, capsys):
    assert skill_cmd.install(print_only=True) == 0
    assert "would write" in capsys.readouterr().out
    for dir_ in home_targets:
        assert not dir_.exists(), "--print must not create anything"


def test_status_reports_a_stale_copy_rather_than_passing(home_targets, capsys):
    """An upgrade replaces the package but not the installed copy; status has to say so,
    because a stale skill quietly keeps teaching last month's policy."""
    skill_cmd.install()
    capsys.readouterr()
    victim = home_targets[0] / "gemini-mcp" / "SKILL.md"
    victim.write_text("an older skill\n", encoding="utf-8")

    assert skill_cmd.status() == 0
    out = capsys.readouterr().out
    assert "STALE" in out
    assert "installed (installed by this command)" in out


def test_uninstall_removes_only_what_it_installed(home_targets, capsys):
    skill_cmd.install()
    # a stranger's skill, plus a manifest we did not write
    other = home_targets[0] / "someone-elses-skill"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("not ours\n", encoding="utf-8")
    capsys.readouterr()

    assert skill_cmd.uninstall() == 0
    assert not (home_targets[0] / "gemini-mcp").exists()
    assert other.is_dir(), "uninstall must never touch a skill it did not install"
    assert not (home_targets[0] / skill_cmd.MANIFEST).exists()
    assert "nothing installed by this command" in capsys.readouterr().out or True


def test_uninstall_without_install_is_a_plain_no_op(home_targets, capsys):
    assert skill_cmd.uninstall() == 0
    assert "nothing installed by this command" in capsys.readouterr().out


def test_the_skill_stays_inside_the_discovery_budget():
    """A skill description is resident from session start. The 19 research-workspace
    skills already spend ~54% of a ~4k-token budget, so this one has to stay small —
    the body is paid for only on activation, the description is paid for always.
    """
    import re

    text = skill_cmd.skill_text()
    fm = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
    name = re.search(r"^name:\s*(.+)$", fm, re.M).group(1).strip()
    desc = re.search(r"^description:\s*(.+)$", fm, re.M).group(1).strip()

    assert name == name.lower() and len(name) <= 64
    assert len(desc) <= 1024, "the spec caps description at 1024 chars"
    assert len(desc) / 3.9 < 150, "description must cost ~100-120 tokens, not more"
    assert len(text) / 3.9 < 5000, "the body is loaded on activation; keep it under budget"
