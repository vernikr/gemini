"""FR-B14: split only if the full TPM cannot hold one measured PDF at its level."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import FakeCtx, FakeRouter, make_app_cfg, make_deps
from gemini_mcp import ingest, tools
from gemini_mcp.config import IngestCfg
from gemini_mcp.progress import ProgressEmitter
from gemini_mcp.tools import analyze_file_tool, analyze_files_tool, file_upload_ticket_tool

PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def cfg(tpm=2500):
    return SimpleNamespace(
        overflow_tiers=["gemini-3.5-flash-lite", "gemma-auto"],
        overflow_limits=SimpleNamespace(rpd=500),
        limits_per_key_model=SimpleNamespace(rpd=20, rpm=5, tpm=tpm),
        quota_day_tz="America/Los_Angeles", window_s=60.0, planned_ttl_s=300.0,
        chain=["gemini-3.8-flash", "gemini-3-flash-preview"],
        api_keys=[FakeRouter.KEY])


def deps_for(tmp_path, monkeypatch, router):
    monkeypatch.setenv("GEMINI_MCP_TOKEN", "tok123")
    app_cfg = make_app_cfg(tmp_path / "logs", chunk_size_tokens=100000).model_copy(
        update={"ingest": IngestCfg(dir=str(tmp_path / "inbox"), use_pandoc=False)})
    monkeypatch.setattr(ingest, "pdf_pages", lambda path, timeout_s=30: 4)
    monkeypatch.setattr(tools.shutil, "which", lambda name: "/usr/bin/qpdf")
    def fake_split(src, dest, first, last):
        dest.write_bytes(PDF + f" pages {first}-{last}".encode())
    monkeypatch.setattr(tools, "split_pages", fake_split)
    return make_deps(router=router, app_cfg=app_cfg)


async def upload(deps, filename, data):
    ticket = await file_upload_ticket_tool(deps, filename, len(data))
    ingest.save_upload(Path(deps.app_cfg.ingest.dir), ticket["upload_id"],
                       data, 50 * 2**20)
    return ticket["upload_id"]


async def single(deps, uid, **kwargs):
    return await analyze_file_tool(deps, ProgressEmitter(FakeCtx()),
                                   {"task": "summarize", "upload_id": uid, **kwargs})


class MeasuredRouter(FakeRouter):
    def __init__(self, *, original=5000, chunk=1200, tpm=2500):
        super().__init__(cfg=cfg(tpm=tpm))
        self.original, self.chunk = original, chunk

    async def count_file_tokens(self, model, uri, mime, *, key_id=None,
                                media_resolution=None):
        self.file_count_levels.append(media_resolution)
        if mime == "image/png":
            return 900
        idx = int(uri.rsplit("/f", 1)[-1]) - 1
        return (self.chunk if " pages " in (self.uploads[idx]["display_name"] or "")
                else self.original)


async def test_split_four_pages_per_file_then_consolidate(tmp_path, monkeypatch):
    router = MeasuredRouter()
    deps = deps_for(tmp_path, monkeypatch, router)
    uid = await upload(deps, "big.pdf", PDF)
    out = await single(deps, uid)
    assert out["meta"]["file"]["split"]["ranges"] == [[1, 1], [2, 2], [3, 3], [4, 4]]
    assert out["meta"]["file"]["split"]["resolution"] == "high"
    assert [c.kind for c in router.calls] == ["chunk"] * 4 + ["consolidate"]
    assert all(len(fs) == 1 for fs in router.files_seen[:4])
    assert all(c.est_tokens <= 2500 for c in router.calls[:4])
    assert out["result"] == "CONSOLIDATED[4]"
    assert out["meta"]["requests_spent"] == 5
    assert out["report"].startswith("file=big.pdf") and "⚠️" in out["report"]
    assert not list(tmp_path.glob("**/part-*.pdf"))


async def test_full_tpm_not_remaining_window_or_badge(tmp_path, monkeypatch):
    router = MeasuredRouter(original=2000)
    deps = deps_for(tmp_path, monkeypatch, router)
    uid = await upload(deps, "fits.pdf", PDF)
    out = await single(deps, uid)
    assert "split" not in out["meta"]["file"]
    assert len(router.uploads) == 1 and len(router.calls) == 1


async def test_unmeasured_piece_fails_before_generate(tmp_path, monkeypatch):
    router = MeasuredRouter(chunk=0)
    deps = deps_for(tmp_path, monkeypatch, router)
    uid = await upload(deps, "unmeasured.pdf", PDF)
    with pytest.raises(ToolError) as exc:
        await single(deps, uid)
    assert json.loads(str(exc.value))["error"] == "pdf_split_over_budget"
    assert router.calls == []


async def test_missing_qpdf_refuses_cleanly(tmp_path, monkeypatch):
    router = MeasuredRouter()
    deps = deps_for(tmp_path, monkeypatch, router)
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    uid = await upload(deps, "big.pdf", PDF)
    with pytest.raises(ToolError) as exc:
        await single(deps, uid)
    assert json.loads(str(exc.value))["error"] == "pdf_split_unavailable"
    assert router.calls == []


async def test_group_split_parts_never_mix_other_files(tmp_path, monkeypatch):
    router = MeasuredRouter(chunk=1200)
    deps = deps_for(tmp_path, monkeypatch, router)
    pdfid = await upload(deps, "big.pdf", PDF)
    pngid = await upload(deps, "image.png", PNG)
    out = await analyze_files_tool(deps, ProgressEmitter(FakeCtx()),
                                   {"task": "summarize", "upload_ids": [pngid, pdfid]})
    assert out["result"] == "CONSOLIDATED[2]"
    assert len(router.files_seen) == 7  # 4 PDF chunks, per-file + group consolidations, PNG
    assert [len(fs) for fs in router.files_seen] == [1, 1, 1, 1, 0, 1, 0]
    assert [f["filename"] for f in out["meta"]["files"]] == ["big.pdf", "image.png"]
    assert out["meta"]["files"][0]["split"]["chunks"] == 4
    assert out["meta"]["requests_spent"] == 7
    assert out["meta"]["parts"] == 2
    trace = out["meta"]["progress_trace"]
    assert [e["pct"] for e in trace] == sorted(e["pct"] for e in trace)
    assert all(e["pct"] < 100 for e in trace[:-1])


def test_report_badges_every_unrequested_lite_or_gemma_model():
    meta = {"backend": "router", "route": "optimal", "thinking_level": "medium",
            "models_used": ["gemini-3.8-flash", "gemini-3.5-flash-lite",
                            "gemma-3-27b", "gemini-3.8-flash"],
            "requested_model": None, "progress_trace": [], "requests_spent": 4}
    report = tools._report(meta)
    assert report.startswith("⚠️")
    assert "models=[gemini-3.8-flash, ⚠️ gemini-3.5-flash-lite, " \
           "⚠️ gemma-3-27b]" in report
    assert "unrequested lite/gemma fallback" in report
    meta["requested_model"] = "gemini-3.5-flash-lite"
    report = tools._report(meta)
    assert "⚠️ gemini-3.5-flash-lite" not in report
    assert "⚠️ gemma-3-27b" in report
    meta["models_used"] = ["gemini-3.5-flash-lite"]
    assert tools._report(meta).startswith("backend=")  # explicitly requested

async def test_measured_piece_bisected_until_it_fits(tmp_path, monkeypatch):
    class UnevenRouter(MeasuredRouter):
        async def count_file_tokens(self, model, uri, mime, *, key_id=None,
                                    media_resolution=None):
            idx = int(uri.rsplit("/f", 1)[-1]) - 1
            name = self.uploads[idx]["display_name"]
            if " pages " not in name:
                return 2800  # span 2 from mean; each 2-page part will be too costly
            return 2200 if name.endswith(("1-2", "3-4")) else 1100
    router = UnevenRouter()
    deps = deps_for(tmp_path, monkeypatch, router)
    uid = await upload(deps, "big.pdf", PDF)
    out = await single(deps, uid)
    assert out["meta"]["file"]["split"]["ranges"] == [[1, 1], [2, 2], [3, 3], [4, 4]]
    assert len(router.calls) == 5 and all(len(fs) == 1 for fs in router.files_seen[:4])


async def test_non_pdf_refused_before_splitting_starts(tmp_path, monkeypatch):
    class OversizeImage(MeasuredRouter):
        async def count_file_tokens(self, model, uri, mime, *, key_id=None,
                                    media_resolution=None):
            return 4000 if mime == "image/png" else await super().count_file_tokens(
                model, uri, mime, key_id=key_id, media_resolution=media_resolution)
    router = OversizeImage()
    deps = deps_for(tmp_path, monkeypatch, router)
    pdfid = await upload(deps, "big.pdf", PDF)
    pngid = await upload(deps, "image.png", PNG)
    with pytest.raises(ToolError) as exc:
        await analyze_files_tool(deps, ProgressEmitter(FakeCtx()),
                                 {"task": "summarize", "upload_ids": [pdfid, pngid]})
    assert json.loads(str(exc.value))["error"] == "group_file_over_budget"
    assert router.calls == []

async def test_failed_tool_payload_requests_blocker_badge():
    async def fail(_emitter, _params):
        raise ToolError('{"error":"pdf_split_failed","hint":"install qpdf"}')
    with pytest.raises(ToolError) as exc:
        await tools._marked_runner(fail, ProgressEmitter(FakeCtx()), {})
    payload = json.loads(str(exc.value))
    assert payload["error"] == "pdf_split_failed"
    assert "⛔️" in payload["agent_notice"]
    assert payload["hint"] == "install qpdf"
