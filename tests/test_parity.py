"""Parity: one scenario, both interfaces, same answer.

The owner's standing rule is that MCP and CLI must not differ in capability or in what
they cost the caller. That is easy to assert and easy to lose: the two surfaces are
wired separately, so a parameter added to one, a default changed in the other, or a meta
field dropped from one envelope drifts apart without any single test failing.

So this drives the *same* job through `analyze_large_tool` (what the MCP server calls)
and through `cli.main(["analyze", …])` (what an agent in OmniRush calls, where MCP does
not exist) and compares what comes back. Both reach the same engine by construction; the
test is here to catch the day that stops being true.
"""
from __future__ import annotations

import io
import json

import pytest

from conftest import FakeCtx, FakeRouter, make_deps
from gemini_mcp import cli
from gemini_mcp.progress import ProgressEmitter
from gemini_mcp.tools import analyze_large_tool

TASK = "summarize the risks"
# Long enough to cross the chunk threshold in the fixture config, so both surfaces are
# exercising the chunked pipeline and not the single-shot shortcut.
BODY = "".join(f"Sentence {i} " + "y" * 24 + ". " for i in range(120))


@pytest.fixture
def wired_cli(monkeypatch, tmp_path):
    """A CLI aimed at a small chunk threshold, with a fake router behind it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_MCP_BACKEND", "direct")
    monkeypatch.setenv("GEMINI_API_KEYS", "AIzaSyDUMMYdummy1234567890ab")
    monkeypatch.setenv("COLUMNS", "100000")        # rich must not wrap the JSON
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "backend: direct\n"
        "pipeline:\n"
        "  chunk_threshold_tokens: 100\n"
        "  chunk_size_tokens: 60\n"
        "  overlap_ratio: 0.2\n"
        "  consolidation_fanin: 2\n"
        "logging:\n"
        f"  dir: {tmp_path / 'logs'}\n", encoding="utf-8")
    buf = io.StringIO(BODY)
    buf.isatty = lambda: False                     # a piped agent: JSON by default
    monkeypatch.setattr("sys.stdin", buf)
    return monkeypatch


async def _via_mcp(router: FakeRouter | None = None) -> dict:
    """backend='direct' matches the CLI leg's config.yaml, so the report line the two
    surfaces print is comparable field for field instead of differing on wiring."""
    out = await analyze_large_tool(make_deps(router=router, backend="direct"),
                                   ProgressEmitter(FakeCtx()),
                                   {"task": TASK, "body": BODY})
    return out


def _via_cli(monkeypatch, router: FakeRouter, *, expect_rc: int = 0) -> int:
    monkeypatch.setattr(cli, "make_routers", lambda a, r: (router, router, "direct"))
    buf = io.StringIO(BODY)
    buf.isatty = lambda: False
    monkeypatch.setattr("sys.stdin", buf)
    rc = cli.main(["--config", "config.yaml", "analyze", "--task", TASK,
                   "--fields", "all"])
    assert rc == expect_rc, f"the CLI leg exited {rc}, expected {expect_rc}"
    return rc


def test_the_same_job_gives_the_same_answer_through_both_interfaces(wired_cli, capsys):
    """Result text and report line must be identical; meta must agree on everything the
    CLI envelope keeps. `progress_trace` is compared too, via --fields all: the CLI
    slims meta by default for context economy, but the underlying record is the same one.
    """
    mcp_out = _via_mcp_sync()

    _via_cli(wired_cli, FakeRouter())
    cli_out = json.loads(capsys.readouterr().out)

    assert cli_out["ok"] is True
    assert cli_out["result"] == mcp_out["result"], "the two surfaces answered differently"
    # The report line carries elapsed_s, which is wall-clock and legitimately differs
    # between two runs. Everything else in it is a claim about the job, so it must match.
    mcp_fields = _report_fields(mcp_out["report"])
    cli_fields = _report_fields(cli_out["report"])
    assert len(mcp_fields) == len(cli_fields), (
        f"the report line changed shape:\n  mcp={mcp_fields}\n  cli={cli_fields}")
    for i, (a, b) in enumerate(zip(mcp_fields, cli_fields, strict=True)):
        if a.endswith("s") and a[:-1].replace(".", "").isdigit():
            continue                      # elapsed_s: wall clock, differs by nature
        assert a == b, f"report field {i} diverged: mcp={a!r} cli={b!r}"

    for key in ("models_used", "requests_spent", "cache_hits", "chunks", "elapsed_s"):
        assert key in mcp_out["meta"], f"MCP meta lost {key}"
        assert key in cli_out["meta"], f"CLI meta lost {key}"
    assert cli_out["meta"]["models_used"] == mcp_out["meta"]["models_used"]
    assert cli_out["meta"]["requests_spent"] == mcp_out["meta"]["requests_spent"]
    assert cli_out["meta"]["chunks"] == mcp_out["meta"]["chunks"]
    # the whole run, stage by stage — same engine, same history
    assert [s["stage"] for s in cli_out["meta"]["progress_trace"]] == \
           [s["stage"] for s in mcp_out["meta"]["progress_trace"]]


def _via_mcp_sync(router: FakeRouter | None = None) -> dict:
    import asyncio

    return asyncio.run(_via_mcp(router))


def test_a_quota_failure_is_the_same_error_on_both_surfaces(wired_cli, capsys):
    """The error vocabulary is one dictionary by construction. If it ever stops being
    one, an agent switching between MCP and CLI would have to learn two failure modes —
    and in OmniRush there is no MCP to fall back to.
    """
    import asyncio

    from mcp.server.mcpserver.exceptions import ToolError

    from gemini_mcp.compute_cmd import EXIT_QUOTA

    with pytest.raises(ToolError) as ei:
        asyncio.run(analyze_large_tool(make_deps(router=FakeRouter(raise_quota=True)),
                                       ProgressEmitter(FakeCtx()),
                                       {"task": TASK, "body": BODY}))
    mcp_err = json.loads(str(ei.value))

    _via_cli(wired_cli, FakeRouter(raise_quota=True), expect_rc=EXIT_QUOTA)
    captured = capsys.readouterr()
    cli_err = json.loads(captured.out)

    assert cli_err["ok"] is False
    assert cli_err["error"]["code"] == mcp_err["error"] == "quota_exhausted"
    # the recovery detail has to survive the trip, or the agent cannot act on it
    assert cli_err["error"]["per_model"] == mcp_err["per_model"]
    assert cli_err["error"]["reset_eta_pt"] == mcp_err["reset_eta_pt"]
    # a JSON failure still goes to stdout; stderr carries only the progress log
    assert not captured.err.startswith("{"), "the envelope must not be split across streams"


def _report_fields(line: str) -> list[str]:
    """' · ' separates the claims, in a fixed order. The last ones carry no name
    ('0.0s', 'clean run'), so this compares by position rather than by key."""
    return [p.strip() for p in line.split(" · ")]
