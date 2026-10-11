"""M4.5 file ingest: HMAC tickets (expiry/tamper/one-use), atomic inbox store,
binary fail-fast with ZERO spend, server-side RTF extraction, analyze_file E2E,
bearer-exempt upload route, three prune triggers. No network (b01)."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import FakeCtx, FakeRouter, make_app_cfg, make_deps
from gemini_mcp import cli, ingest
from gemini_mcp.config import IngestCfg
from gemini_mcp.progress import ProgressEmitter
from gemini_mcp.server import build_http_app, build_server
from gemini_mcp.tools import analyze_file_tool, file_upload_ticket_tool

TOKEN = "tok123"

RTF_SAMPLE = (b"{\\rtf1\\ansi\\ansicpg1251\\deff0\n"
              b"{\\fonttbl{\\f0\\fswiss Arial;}}\n"
              b"Hello \\'d0\\'e5\\'ea\\'e0\\par\n"
              b"Second\\uc1\\u1055 ?rend\\line\n"
              b"{\\colortbl ;\\red0\\green0\\blue0;}\n"
              b"End}")


def _deps(tmp_path, monkeypatch, router=None, **ingest_kw):
    monkeypatch.setenv("GEMINI_MCP_TOKEN", TOKEN)
    cfg = make_app_cfg(tmp_path / "logs").model_copy(
        update={"ingest": IngestCfg(dir=str(tmp_path / "inbox"),
                                    scp_target="root@vps",
                                    use_pandoc=False,      # deterministic E2E
                                    **ingest_kw)})
    return make_deps(router=router or FakeRouter(), app_cfg=cfg)


def test_pandoc_markdown_via_stub(tmp_path, monkeypatch):
    """Real subprocess against a stub 'pandoc' on PATH: RTF → gfm Markdown."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "pandoc"
    stub.write_text("#!/bin/sh\nprintf '# Заголовок\\n\\n**Жирный** текст\\n'\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    text, fmt, extractor = ingest.extract_text(RTF_SAMPLE, "doc.rtf")
    assert fmt == "markdown" and extractor == "pandoc/rtf→gfm"
    assert text.startswith("# Заголовок") and "**Жирный**" in text


def test_pandoc_missing_rtf_falls_back_builtin(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))       # empty dir → no pandoc
    text, enc, extractor = ingest.extract_text(RTF_SAMPLE, "doc.rtf")
    assert extractor == "rtf" and "Hello Река" in text


def test_docx_container_without_pandoc_fails_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert ingest.sniff_binary(b"PK\x03\x04junk", "f.docx") is None  # ext-gated pass
    with pytest.raises(ingest.IngestError) as e:
        ingest.extract_text(b"PK\x03\x04junk", "f.docx")
    assert e.value.payload["error"] == "conversion_unavailable"
    # pandoc disabled entirely → sniffer rejects the container up front
    assert ingest.sniff_binary(b"PK\x03\x04junk", "f.docx", pandoc_ok=False) is not None


def test_ticket_sign_verify_tamper_expiry():
    sig = ingest.make_ticket(TOKEN, "id1", 9999999999, 10)
    ingest.verify_ticket(TOKEN, "id1", "9999999999", "10", sig)
    with pytest.raises(ingest.IngestError) as e:
        ingest.verify_ticket(TOKEN, "id1", "9999999999", "11", sig)   # size tamper
    assert e.value.payload["error"] == "bad_signature"
    exp = int(time.time()) - 5
    with pytest.raises(ingest.IngestError) as e:
        ingest.verify_ticket(TOKEN, "id1", exp, 10,
                             ingest.make_ticket(TOKEN, "id1", exp, 10))
    assert e.value.payload["error"] == "ticket_expired"


def test_rtf_extraction():
    text = ingest.rtf_to_text(RTF_SAMPLE.decode("latin-1"))
    assert "Hello Река" in text                 # \'xx runs → cp1251
    assert "SecondПrend" in text                # \uc1 + \u1055 + fallback skip
    assert "Arial" not in text and "colortbl" not in text   # junk groups skipped
    assert text.rstrip().endswith("End")


def test_sniff_binary():
    assert ingest.sniff_binary(b"%PDF-1.7 rest") == "pdf"
    assert ingest.sniff_binary(b"PK\x03\x04zip") == "zip-container (docx/xlsx/…)"
    assert "nul" in ingest.sniff_binary(b"abc\x00def")
    assert ingest.sniff_binary("привет, text!".encode()) is None


def test_prune_removes_old(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "aaa").write_bytes(b"x")
    os.utime(inbox / "aaa", (0, 0))
    (inbox / "aaa.json").write_text("{}")
    (inbox / "bbb").write_bytes(b"y")
    assert ingest.prune(inbox, keep_hours=1, total_cap_mb=10) == 1
    assert not (inbox / "aaa").exists() and (inbox / "bbb").exists()


def test_prune_clears_spent_markers_and_orphan_parts(tmp_path):
    """FR-E9: markers, sidecars and half-written parts outlive their blob when it was
    removed outside prune; the inbox must not accumulate them forever."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ("a.consumed", "a.json", "b.part"):
        p = inbox / name
        p.write_text("x")
        os.utime(p, (0, 0))
    live = inbox / "c.consumed"                 # young: belongs to an open ticket
    live.write_text("x")
    assert ingest.prune(inbox, keep_hours=1, total_cap_mb=10) == 0
    for name in ("a.consumed", "a.json", "b.part"):
        assert not (inbox / name).exists()
    assert live.exists()


def test_spent_ticket_is_refused_without_the_blob(tmp_path):
    """FR-E9: the one-time guarantee is the ticket, not the file — after the claim the
    same ticket is refused even once the blob is gone (prune, eviction, cleanup)."""
    inbox = tmp_path / "inbox"
    ingest.reserve(inbox, "u1", "a.txt", 3)
    ingest.save_upload(inbox, "u1", b"abc", 99)
    assert ingest.ticket_spent(inbox, "u1")
    (inbox / "u1").unlink()
    with pytest.raises(ingest.IngestError) as e:
        ingest.save_upload(inbox, "u1", b"abc", 99)
    assert e.value.payload["error"] == "ticket_consumed"
    assert not (inbox / "u1").exists()          # the refused replay wrote nothing


async def test_upload_route_one_time_and_analyze_file(tmp_path, monkeypatch):
    deps = _deps(tmp_path, monkeypatch)
    ticket = await file_upload_ticket_tool(deps, "note.txt", 11)
    assert ticket["scp_path"].startswith("root@vps:")
    assert "curl -T" in ticket["instructions"]
    srv = build_server(deps)
    app = build_http_app(srv, deps.app_cfg, TOKEN)
    u = urlsplit(ticket["upload_url"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        r = await c.put(f"{u.path}?{u.query}", content=b"hello world")  # no bearer
        assert r.status_code == 200 and r.json()["bytes"] == 11
        r2 = await c.put(f"{u.path}?{u.query}", content=b"hello world")
        assert r2.status_code == 409 and r2.json()["error"] == "ticket_consumed"
        q3 = u.query.rsplit("sig=", 1)[0] + "sig=deadbeef"
        r3 = await c.put(f"{u.path}?{q3}", content=b"x")
        assert r3.status_code == 400                       # tampered sig
    out = await analyze_file_tool(deps, ProgressEmitter(FakeCtx()),
                                  {"task": "t", "upload_id": ticket["upload_id"]})
    assert out["result"].startswith("SINGLE[")
    assert out["meta"]["file"]["filename"] == "note.txt"
    assert out["meta"]["file"]["extractor"] == "plain"
    assert out["report"].startswith("file=note.txt")
    assert "route=optimal" in out["report"] and "clean run" in out["report"]


def test_cap_eviction_keeps_the_spent_marker(tmp_path):
    """Evicting a blob to stay under the total cap must not hand its ticket back: the
    claim is what refuses the replay, and it outlives the bytes."""
    inbox = tmp_path / "inbox"
    ingest.reserve(inbox, "blob", "a.txt", 3)
    ingest.save_upload(inbox, "blob", b"x" * 4096, 99_999)
    assert ingest.prune(inbox, keep_hours=24, total_cap_mb=0.001) == 1   # 1 KB cap
    assert not (inbox / "blob").exists()
    assert ingest.ticket_spent(inbox, "blob")
    with pytest.raises(ingest.IngestError) as e:
        ingest.save_upload(inbox, "blob", b"x", 99_999)
    assert e.value.payload["error"] == "ticket_consumed"


async def test_route_refuses_a_replayed_ticket_without_the_blob(tmp_path, monkeypatch):
    """FR-E9, route level. The replay window used to be the whole TTL: with the blob
    gone (prune, cap eviction, manual cleanup) the signed URL was accepted again."""
    deps = _deps(tmp_path, monkeypatch)
    ticket = await file_upload_ticket_tool(deps, "note.txt", 11)
    srv = build_server(deps)
    app = build_http_app(srv, deps.app_cfg, TOKEN)
    u = urlsplit(ticket["upload_url"])
    inbox = Path(deps.app_cfg.ingest.dir)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        r = await c.put(f"{u.path}?{u.query}", content=b"hello world")
        assert r.status_code == 200
        (inbox / ticket["upload_id"]).unlink()
        r2 = await c.put(f"{u.path}?{u.query}", content=b"other bytes")
    assert r2.status_code == 409 and r2.json()["error"] == "ticket_consumed"
    assert not (inbox / ticket["upload_id"]).exists()


async def test_analyze_file_prunes_a_stale_inbox(tmp_path, monkeypatch):
    """FR-E9 second prune trigger: before it, files outlived their retention unless
    the client happened to request another ticket."""
    deps = _deps(tmp_path, monkeypatch)
    inbox = Path(deps.app_cfg.ingest.dir)
    ticket = await file_upload_ticket_tool(deps, "note.txt", 5)
    ingest.save_upload(inbox, ticket["upload_id"], b"hello", 99)
    stale = [inbox / n for n in ("old-blob", "old-blob.json", "old-blob.consumed")]
    for p in stale:
        p.write_text("x")
        os.utime(p, (0, 0))
    out = await analyze_file_tool(deps, ProgressEmitter(FakeCtx()),
                                  {"task": "t", "upload_id": ticket["upload_id"]})
    assert out["result"].startswith("SINGLE[")
    assert not any(p.exists() for p in stale)


async def test_binary_fail_fast_zero_spend(tmp_path, monkeypatch):
    """An unsupported container still fails fast with ZERO spend. PDFs used to be the
    example here; they are supported now (provider Files API — see test_files_api.py),
    so this pins the rule on what stays unsupported."""
    router = FakeRouter()
    deps = _deps(tmp_path, monkeypatch, router=router)
    ticket = await file_upload_ticket_tool(deps, "archive.gz", 8)
    ingest.save_upload(Path(deps.app_cfg.ingest.dir), ticket["upload_id"],
                       b"\x1f\x8b\x08\x00binary", 50 * 2**20)
    with pytest.raises(ToolError) as ei:
        await analyze_file_tool(deps, ProgressEmitter(FakeCtx()),
                                {"task": "t", "upload_id": ticket["upload_id"]})
    assert "binary_file_needs_conversion" in str(ei.value)
    assert "PDF, images, audio" in str(ei.value)     # the hint names what IS readable
    assert not router.calls and not router.uploads   # ZERO quota spend, no upload


async def test_analyze_file_rtf_end_to_end(tmp_path, monkeypatch):
    deps = _deps(tmp_path, monkeypatch)
    ticket = await file_upload_ticket_tool(deps, "doc.rtf", len(RTF_SAMPLE))
    ingest.save_upload(Path(deps.app_cfg.ingest.dir), ticket["upload_id"],
                       RTF_SAMPLE, 50 * 2**20)
    out = await analyze_file_tool(deps, ProgressEmitter(FakeCtx()),
                                  {"task": "t", "upload_id": ticket["upload_id"]})
    assert out["meta"]["file"]["extractor"] == "rtf"
    assert "via rtf" in out["report"]


async def test_analyze_file_missing_bytes(tmp_path, monkeypatch):
    deps = _deps(tmp_path, monkeypatch)
    ticket = await file_upload_ticket_tool(deps, "never.txt", 5)
    with pytest.raises(ToolError) as ei:
        await analyze_file_tool(deps, ProgressEmitter(FakeCtx()),
                                {"task": "t", "upload_id": ticket["upload_id"]})
    assert "upload_not_found" in str(ei.value)


# ------------------------------------------------- size cap and its escape hatch
async def test_ticket_refuses_over_cap_with_the_three_alternatives(tmp_path,
                                                                   monkeypatch):
    """The refusal has to tell the agent what to do instead, not just say no."""
    deps = _deps(tmp_path, monkeypatch, max_file_mb=1)
    with pytest.raises(ToolError) as ei:
        await file_upload_ticket_tool(deps, "big.txt", 3 * 1_048_576)
    payload = json.loads(str(ei.value))
    assert payload["error"] == "file_too_large"
    assert payload["max_bytes"] == 1_048_576
    for word in ("compress", "split", "explicit permission", "override_size_limit"):
        assert word in payload["fix"], payload["fix"]


async def test_oversize_only_needs_the_callers_word_and_leaves_a_record(tmp_path,
                                                                       monkeypatch):
    """There is no server-side switch: override_size_limit is the whole gate, so the
    compensating control is that the issuance is logged."""
    deps = _deps(tmp_path, monkeypatch, max_file_mb=1)
    records = []

    class _Cap(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())

    deps.logger.addHandler(_Cap())
    deps.logger.setLevel(logging.WARNING)

    ticket = await file_upload_ticket_tool(deps, "big.txt", 3 * 1_048_576,
                                           override_size_limit=True)
    assert ticket["oversize"] is True and ticket["upload_id"]
    assert any("OVERSIZE" in m and "big.txt" in m and "3145728" in m for m in records), records

    # the signed ticket is the authority on size, so the route accepts it
    srv = build_server(deps)
    app = build_http_app(srv, deps.app_cfg, TOKEN)
    u = urlsplit(ticket["upload_url"])
    body = b"z" * (3 * 1_048_576)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        r = await c.put(f"{u.path}?{u.query}", content=body)
    assert r.status_code == 200 and r.json()["bytes"] == len(body)

    # a body bigger than the ticket still refused, and nothing written for it
    t2 = await file_upload_ticket_tool(deps, "big2.txt", 2 * 1_048_576,
                                       override_size_limit=True)
    u2 = urlsplit(t2["upload_url"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        r2 = await c.put(f"{u2.path}?{u2.query}", content=b"q" * (3 * 1_048_576))
    assert r2.status_code == 413 and r2.json()["error"] == "file_too_large"


def test_cli_prune_also_empties_the_inbox(monkeypatch, tmp_path, capsys):
    """FR-E9 third prune trigger: `prune` covered logs and audit rows while the inbox
    — the one place files accumulate — waited for a ticket that might never come."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "blob").write_bytes(b"x")
    os.utime(inbox / "blob", (0, 0))
    cfg = make_app_cfg(tmp_path / "logs").model_copy(
        update={"ingest": IngestCfg(dir=str(inbox))})
    monkeypatch.setattr(cli, "prune_jsonl", lambda lcfg, dry: 0)
    monkeypatch.setattr(cli, "make_routers", lambda a, r: (
        SimpleNamespace(ledger=SimpleNamespace(prune=lambda **k: {})), None, "direct"))
    monkeypatch.setattr(cli, "enforce_audit_size", lambda *a: {})
    assert cli._run_prune(cfg, SimpleNamespace(cache_ttl_h=24),
                          SimpleNamespace(audit_days=None, dry=False)) == 0
    assert json.loads(capsys.readouterr().err)["inbox_removed"] == 1
    assert not (inbox / "blob").exists()


def test_default_cap_is_twenty_megabytes():
    """Guards the measured ceiling: peak RSS runs about 4.6 MB per uploaded MB, so a
    350 MB MemoryMax is reached near 60 MB and the default must stay well under it."""
    from gemini_mcp.config import AppCfg

    cfg = AppCfg().ingest
    assert cfg.max_file_mb == 20
    assert not hasattr(cfg, "allow_oversize")     # the gate is the user's word, not a flag
