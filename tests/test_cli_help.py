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
