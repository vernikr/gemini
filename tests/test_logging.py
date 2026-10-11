"""Logging & observability (FR-E4..E8): redaction, rotation file, JSONL audit,
retention pruning, emitter degradation (b03)."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from gemini_router import RouterEvent

from conftest import FakeCtx
from gemini_mcp.config import LoggingCfg
from gemini_mcp.logging_setup import (
    AuditLogger,
    RedactionFilter,
    prune_jsonl,
    redact,
    setup_logging,
)
from gemini_mcp.progress import ProgressEmitter


def test_redact_patterns_and_secrets():
    out = redact("key AIzaSyDUMMYtestvalue1234567890gh and "
                 "ghp_DUMMYghpvalueabcdefghijklmnopqrstuvwxyz012345"
                 " token longsecretvalue", ["longsecretvalue"])
    assert "AIza" not in out and "ghp_" not in out and "longsecretvalue" not in out
    assert out.count("***") >= 3


def test_redact_covers_the_aq_key_generation():
    """AI Studio hands out `AQ.…` keys; the redactor knew only the classic `AIza…` shape,
    so an AQ. key could reach the log in clear text. Both generations now redact."""
    out = redact("key AQ.DUMMYtestvalue1234567890abcdef and done")
    assert "AQ." not in out and "DUMMYtestvalue" not in out and "***" in out


def test_redaction_filter_rewrites_record():
    f = RedactionFilter(["tok-12345678"])
    rec = logging.LogRecord("x", logging.INFO, "f", 1,
                            "key AIzaSyABCDEFGHIJ12345 and tok-12345678", None, None)
    assert f.filter(rec)
    msg = rec.getMessage()
    assert "AIza" not in msg and "tok-12345678" not in msg and "***" in msg


def test_setup_logging_writes_rotating_file(tmp_path):
    cfg = LoggingCfg(dir=str(tmp_path))
    logger = setup_logging(cfg, secrets=[])
    logger.info("hello AIzaSySECRETKEY12345 world")
    for h in logger.handlers:
        h.flush()
    content = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert "hello" in content and "AIzaSySECRETKEY" not in content


def test_audit_logger_record_tail_and_secrets(tmp_path):
    cfg = LoggingCfg(dir=str(tmp_path))
    audit = AuditLogger(cfg, secrets=["verysecrettoken"])
    audit.record({"tool": "analyze_large", "note": "contains verysecrettoken inside"})
    audit.record({"tool": "quota_status"})
    entries = audit.tail(10)
    assert len(entries) == 2 and entries[0]["tool"] == "analyze_large"
    raw = audit.path_for().read_text(encoding="utf-8")
    assert "verysecrettoken" not in raw and "***" in raw
    assert audit.tail(1)[0]["tool"] == "quota_status"


def test_prune_jsonl_evicts_old(tmp_path):
    cfg = LoggingCfg(dir=str(tmp_path), jsonl_keep_days=14)
    old = tmp_path / "requests-2020-01-01.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    fresh = AuditLogger(cfg).path_for()
    fresh.write_text("{}\n", encoding="utf-8")
    removed = prune_jsonl(cfg)
    assert str(old) in removed and not old.exists() and fresh.exists()
    assert prune_jsonl(cfg, dry=True) == []


async def test_emitter_full_and_degraded():
    ctx = FakeCtx()
    em = ProgressEmitter(ctx)
    await em.stage("plan", 10, "planning 5 chunks")
    assert ctx.progress[0] == (10.0, 100.0, "planning 5 chunks")
    assert em.trace[-1]["stage"] == "plan"

    em.router_event_sync(RouterEvent(stage="waiting", model="gemini-3.8-flash",
                                     wait_s=42, detail={"reason": "rpm"}))
    await em.drain()
    assert em.trace[-1]["stage"] == "router:waiting"
    assert any("waiting 42s" in m for m in ctx.messages)          # countdown surfaced

    degraded = ProgressEmitter(None)                              # no ctx → trace only
    await degraded.stage("x", 5, "still works")
    degraded.router_event_sync(RouterEvent(stage="ok", model="m"))
    assert len(degraded.trace) == 2


def test_progress_pct_monotonic_guard():
    em = ProgressEmitter(None)
    import asyncio
    async def run():
        await em.stage("a", 10)
        await em.stage("b")            # no pct → keeps 10
        await em.stage("c", 90)
    asyncio.run(run())
    pcts = [t["pct"] for t in em.trace]
    assert pcts == [10.0, 10.0, 90.0]


def test_audit_path_date_format(tmp_path):
    cfg = LoggingCfg(dir=str(tmp_path))
    path = AuditLogger(cfg).path_for(time.time())
    assert path.name.startswith("requests-") and path.name.endswith(".jsonl")


def test_enforce_audit_size_reaches_its_cap(tmp_path):
    """The cap is real: prune alone cannot shrink a SQLite file, VACUUM does."""
    from gemini_router import Ledger

    from gemini_mcp.logging_setup import enforce_audit_size

    led = Ledger(tmp_path / "state.db")
    now = time.time()
    for _ in range(4000):                      # ~0.5 MB of rows, all outside 30 days
        rid = led.plan(key_id="k", model="m", kind="single", est_tokens=10,
                       request_id=None, source="mcp", now=now - 60 * 86400)
        led.resolve(rid, "ok", tokens_in=10, tokens_out=10)
    over = Path(led.path).stat().st_size / 1e6
    assert over > 0.1

    out = enforce_audit_size(led, max_mb=0.1)
    assert out["cap_met"], out
    assert out["keep_days_used"] == 30.0       # the mildest step was enough
    assert out["size_mb"] <= 0.1
    assert led.stats()["calls"] == 0

    # a db already under the cap is left alone
    assert enforce_audit_size(led, max_mb=50) == {"cap_met": True, "keep_days_used": None,
                                                  "size_mb": out["size_mb"]}
