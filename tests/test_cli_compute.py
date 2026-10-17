"""CLI compute subcommands (`ask`, `analyze`): the shell contract.

These drive the real `cli.main()` and the real `*_tool()` functions — only the router
is faked — so what is exercised is the code path an agent actually hits: argv parsing,
body loading, the JSON envelope, and the exit-code taxonomy.

FR mapping: BR4 (one core, several consumers) — the CLI must reach the same engine the
MCP server reaches, and must not grow a second one.
"""
from __future__ import annotations

import io
import json

import pytest

from conftest import FakeRouter
from gemini_mcp import cli
from gemini_mcp.compute_cmd import EXIT_QUOTA, EXIT_REFUSED, EXIT_USAGE


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A wired CLI whose router is a fake, keys present, logs under tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_MCP_BACKEND", "direct")
    monkeypatch.setenv("GEMINI_API_KEYS", "AIzaSyDUMMYdummy1234567890ab")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "100000")   # rich must not wrap JSON mid-object
    # A small chunk threshold, so a modest test body really exercises the chunked
    # path instead of silently taking the single-shot branch.
    (tmp_path / "config.yaml").write_text(
        "backend: direct\n"
        "pipeline:\n"
        "  chunk_threshold_tokens: 100\n"
        "  chunk_size_tokens: 60\n"
        "  overlap_ratio: 0.2\n"
        "  consolidation_fanin: 2\n"
        "logging:\n"
        f"  dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    return monkeypatch


def _wire(monkeypatch, router: FakeRouter) -> None:
    """Point the CLI at the fake router. Logging and the audit ledger stay real and
    land under tmp_path — the contract under test includes them."""
    monkeypatch.setattr(cli, "make_routers", lambda a, r: (router, router, "direct"))


def _run(argv: list[str]) -> int:
    """cli.main with the fixture's config already in front of the subcommand."""
    return cli.main(["--config", "config.yaml", *argv])


def _stdout(monkeypatch, text: str) -> io.StringIO:
    buf = io.StringIO(text)
    buf.isatty = lambda: False          # a piped agent: JSON is the default
    monkeypatch.setattr("sys.stdin", buf)
    return buf


# ------------------------------------------------------------------ ask

def test_ask_reads_the_prompt_from_stdin_and_prints_one_json_object(env, capsys):
    router = FakeRouter()
    _wire(env, router)
    _stdout(env, "what is the capital of Peru")

    rc = cli.main(["ask"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["report"]                      # the relay line is present
    assert payload["result"].startswith("SINGLE[")
    assert "meta" in payload


def test_ask_never_takes_the_body_from_argv_when_a_file_is_given(env, tmp_path, capsys):
    router = FakeRouter()
    _wire(env, router)
    p = tmp_path / "prompt.txt"
    p.write_text("prompt from a file", encoding="utf-8")

    rc = cli.main(["ask", "--prompt-file", str(p)])

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert router.calls and "prompt from a file" in router.calls[0].prompt


def test_ask_without_a_prompt_fails_with_a_parseable_envelope_and_usage_code(env, capsys):
    """The contract: the exit code says it failed, stdout still carries ONE parseable
    object naming the code and the fix. An agent never has to read prose to recover."""
    _wire(env, FakeRouter())
    _stdout(env, "")                     # empty stdin, no flag

    rc = cli.main(["ask"])

    captured = capsys.readouterr()
    assert rc == EXIT_USAGE
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_prompt"
    assert payload["error"]["fix"]          # the message teaches the next move


def test_ask_reports_quota_as_its_own_exit_code(env, capsys):
    """The distinction an agent acts on: 5 means wait, 2 means never re-run this."""
    _wire(env, FakeRouter(raise_quota=True))
    _stdout(env, "anything")

    rc = cli.main(["ask"])

    captured = capsys.readouterr()
    assert rc == EXIT_QUOTA
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "quota_exhausted"


# -------------------------------------------------------------- analyze

def test_analyze_streams_the_body_through_the_chunked_pipeline(env, tmp_path, capsys):
    router = FakeRouter()
    _wire(env, router)
    _stdout(env, "")
    body = tmp_path / "body.txt"
    body.write_text("x" * 900, encoding="utf-8")   # over the fake's 100-token threshold

    rc = _run(["analyze", "--task", "summarize", "--body-file", str(body)])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["result"].startswith("CONSOLIDATED[")   # the chunked path ran
    assert len(router.calls) > 1                           # more than a single shot


def test_analyze_without_a_task_is_refused_before_any_spend(env, tmp_path, capsys):
    router = FakeRouter()
    _wire(env, router)
    _stdout(env, "")
    body = tmp_path / "body.txt"
    body.write_text("some body", encoding="utf-8")

    rc = _run(["analyze", "--body-file", str(body)])

    assert rc == EXIT_USAGE
    assert router.calls == []              # nothing was sent, so no quota moved


def test_analyze_refuses_a_raw_document_inlined_into_a_text_body(env, tmp_path, capsys):
    """The guard is the core's, reached through the CLI — not reimplemented here."""
    _wire(env, FakeRouter())
    _stdout(env, "")
    body = tmp_path / "doc.rtf"
    body.write_text("{\\rtf1 not really text", encoding="utf-8")

    rc = _run(["analyze", "--task", "t", "--body-file", str(body)])

    payload = json.loads(capsys.readouterr().out)
    assert rc == EXIT_REFUSED               # refused by shape: changing the input helps
    assert payload["error"]["code"] == "raw_document_inlined"


def test_the_file_lane_refuses_plainly_instead_of_pretending(env, tmp_path, capsys):
    _wire(env, FakeRouter())
    _stdout(env, "")

    rc = _run(["analyze", "--task", "t", "-f", str(tmp_path / "x.pdf")])

    assert rc == EXIT_USAGE
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "not_supported"
    assert payload["error"]["fix"]          # the message names the way forward


# ------------------------------------------------------------- contract

def test_meta_is_slim_by_default_and_complete_on_request(env, tmp_path, capsys):
    """Bounded output: a routine call must not return the whole progress trace."""
    _wire(env, FakeRouter())
    _stdout(env, "")
    body = tmp_path / "body.txt"
    body.write_text("x" * 900, encoding="utf-8")

    _run(["analyze", "--task", "t", "--body-file", str(body)])
    slim = json.loads(capsys.readouterr().out)["meta"]

    _run(["analyze", "--task", "t", "--body-file", str(body), "--fields", "all"])
    full = json.loads(capsys.readouterr().out)["meta"]

    assert "progress_trace" not in slim
    assert "progress_trace" in full
    assert set(slim) < set(full)


def test_human_readable_when_stdout_is_a_terminal(env, capsys):
    router = FakeRouter()
    _wire(env, router)
    stdin = io.StringIO("hello there")
    stdin.isatty = lambda: False           # piped body…
    env.setattr("sys.stdin", stdin)
    tty = io.StringIO()
    tty.isatty = lambda: True              # …but a person is watching stdout
    env.setattr("sys.stdout", tty)

    rc = cli.main(["ask"])

    text = tty.getvalue()
    assert rc == 0
    assert not text.strip().startswith("{")  # prose, not JSON
    assert "SINGLE" in text          # the answer itself, in prose
