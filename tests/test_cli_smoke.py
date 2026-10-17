"""`gemini smoke`: the free steps run, and it spends exactly one call."""
from __future__ import annotations

from types import SimpleNamespace

from gemini_mcp import cli
from gemini_mcp.cli import main


class _Router:
    """Stands in for the router; records how many paid calls were made."""

    def __init__(self, text: str = "OK") -> None:
        self.text = text
        self.counted = 0
        self.paid = 0

    async def count_tokens(self, text: str) -> int:
        self.counted += 1
        return len(text) // 4

    async def aclose(self) -> None:
        pass

    def complete_sync(self, req) -> SimpleNamespace:
        self.paid += 1
        return SimpleNamespace(model="gemini-3.8-flash", key_id="b4a7451b", cached=False,
                               tokens_in=12, tokens_out=1, tokens_thought=0,
                               finish_reason="STOP", text=self.text)


def _patch(monkeypatch, text: str = "OK") -> _Router:
    fake = _Router(text)
    monkeypatch.setattr(cli, "make_routers", lambda a, r: (fake, None, "direct"))
    monkeypatch.setattr(cli, "cmd_doctor", lambda args, cfg=None: 0)
    monkeypatch.setattr(cli, "_run_quota", lambda a, r, args: 0)
    return fake


def test_smoke_spends_exactly_one_call(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_MCP_BACKEND", "direct")
    monkeypatch.setenv("GEMINI_API_KEYS", "AIzaSyDUMMYdummy1234567890ab")
    fake = _patch(monkeypatch)

    rc = main(["smoke"])

    captured = capsys.readouterr()
    assert rc == 0
    assert fake.counted == 1                 # free pre-flight ran
    assert fake.paid == 1                    # and exactly one call was spent
    assert "SMOKE OK" in captured.err        # console writes to stderr


def test_smoke_fails_loudly_on_an_unexpected_answer(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_MCP_BACKEND", "direct")
    monkeypatch.setenv("GEMINI_API_KEYS", "AIzaSyDUMMYdummy1234567890ab")
    _patch(monkeypatch, text="")             # thinking-starved empty answer

    rc = main(["smoke"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "SMOKE FAILED" in err


def test_smoke_stops_before_spending_when_doctor_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_MCP_BACKEND", "direct")
    monkeypatch.setenv("GEMINI_API_KEYS", "AIzaSyDUMMYdummy1234567890ab")
    fake = _Router()
    monkeypatch.setattr(cli, "make_routers", lambda a, r: (fake, None, "direct"))
    monkeypatch.setattr(cli, "cmd_doctor", lambda args, cfg=None: 1)

    rc = main(["smoke", "--offline"])

    assert rc == 1
    assert fake.paid == 0                    # no quota burned on a broken setup
    assert fake.counted == 0
