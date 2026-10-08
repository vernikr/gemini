"""M4.5 file ingest: HMAC tickets (expiry/tamper/one-time), atomic inbox store,
binary fail-fast with ZERO spend, server-side RTF extraction, analyze_file E2E,
bearer-exempt upload route. No network (b01)."""
from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import FakeCtx, FakeRouter, make_app_cfg, make_deps
from gemini_mcp import ingest
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
        assert r2.status_code == 409                       # one-time
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


async def test_binary_fail_fast_zero_spend(tmp_path, monkeypatch):
    router = FakeRouter()
    deps = _deps(tmp_path, monkeypatch, router=router)
    ticket = await file_upload_ticket_tool(deps, "doc.pdf", 8)
    ingest.save_upload(Path(deps.app_cfg.ingest.dir), ticket["upload_id"],
                       b"%PDF-1.7", 50 * 2**20)
    with pytest.raises(ToolError) as ei:
        await analyze_file_tool(deps, ProgressEmitter(FakeCtx()),
                                {"task": "t", "upload_id": ticket["upload_id"]})
    assert "binary_file_needs_conversion" in str(ei.value)
    assert not router.calls                                # ZERO quota spend


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
