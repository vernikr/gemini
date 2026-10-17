"""The CLI owes a way back from a typo: a bare invocation answers with the map, a mistyped
command or flag names the nearest real one. Pinned because the vocabulary grows (`keys`,
`smoke`, `prune`… joined recently) and argparse's default answer — a wall of `choose from
…` on stderr — is the kind of thing that stays broken because nobody tests it."""
from __future__ import annotations

import pytest

from gemini_mcp.cli import main


def _run(capsys, argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code, capsys.readouterr()


def test_a_bare_invocation_is_a_question_and_gets_the_map(capsys):
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "usage: gemini-mcp" in out and "common commands:" in out
    assert "gemini-mcp setup" in out and "start here" in out


def test_a_mistyped_command_names_the_real_one(capsys):
    code, io = _run(capsys, ["keyz"])
    assert code == 2 and "did you mean 'keys'?" in io.err
    assert "gemini-mcp --help" in io.err


def test_a_mistyped_subcommand_action_names_the_real_one(capsys):
    code, io = _run(capsys, ["keys", "removee", "1"])
    assert code == 2 and "did you mean 'remove'?" in io.err


def test_a_mistyped_flag_names_the_real_one(capsys):
    # note: argparse accepts unambiguous prefixes (--offlin works), so this is a real typo
    code, io = _run(capsys, ["doctor", "--offlie"])
    assert code == 2 and "did you mean '--offline'?" in io.err


def test_an_unknown_flag_still_gets_a_way_out(capsys):
    code, io = _run(capsys, ["--tocken", "stdio"])
    assert code == 2 and "gemini-mcp --help" in io.err


def test_help_lists_the_recently_added_commands(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    out = capsys.readouterr().out
    assert e.value.code == 0
    for word in ("keys", "smoke", "prune", "doctor"):
        assert word in out


# ------------------------------------------------------------------ the secrets guard
def test_the_guard_flags_a_real_shaped_key_and_accepts_marked_fakes(tmp_path):
    """The guard exists because on 2026-10-11 two real keys were committed into a public
    test fixture and the vendor reported the first as leaked within the hour. The rule for
    fixtures: real shape, DUMMY marker. Pinned both directions."""
    from gemini_mcp import secrets_guard as guard

    # built at runtime so the guard scanning THIS file stays happy (it is its own test)
    sample = "AIzaSy" + "Xk3vQpZ1mN8rT4bY6wL0cD2fG7hJ9sA5e"
    assert guard.scan_text(f'K = "{sample}"', "f") != []
    split = 'K = "ghp_" + "abcdefghijklmnopqrstuvwxyz012345"'
    assert guard.scan_text(split, "f") == []          # concatenation hides it from the scan
    gh = "ghp_" + "abcdefghijklmnopqrstuvwxyz012345"
    assert guard.scan_text(f"K = \"{gh}\"", "f") != []
    assert guard.scan_text('K = "AIzaSyDUMMYfake_for_tests_00000000"', "f") == []
    assert guard.scan_text('K = "AQ.DUMMYfake_for_tests_000000000000000"', "f") == []
    assert guard.scan_text("# a comment mentioning AIza… and ghp_…", "f") == []


def test_the_repo_itself_is_clean(capsys):
    """Runs the same scan the CI step runs — over every tracked file."""
    from gemini_mcp import secrets_guard as guard

    assert guard.main([]) == 0
    assert "clean" in capsys.readouterr().out


# ------------------------------------------------- one length policy, every surface
def test_the_length_policy_is_one_text_on_every_surface(capsys):
    """The owner's rule: MCP and CLI must not differ in capability or in context. A
    policy stated twice drifts — the MCP instructions said "answer in 3-5 sentences"
    while the CLI said nothing at all. Both now quote one canonical string, so this
    fails the moment either surface paraphrases it.
    """
    from gemini_mcp.prompts import BREVITY_RULE
    from gemini_mcp.server import INSTRUCTIONS
    from gemini_mcp.skill_cmd import skill_text

    surfaces = {"MCP instructions": INSTRUCTIONS, "skill": skill_text()}

    for cmd in ("ask", "analyze"):
        with pytest.raises(SystemExit) as e:
            main([cmd, "--help"])
        assert e.value.code == 0
        surfaces[f"`gemini-mcp {cmd} --help`"] = capsys.readouterr().out

    for name, text in surfaces.items():
        assert BREVITY_RULE in text, f"{name} lost the canonical length rule"
