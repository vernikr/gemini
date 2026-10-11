"""Provider-bound file input (docs/03 §10, option A): PDFs, images, audio and video go to
the Gemini Files API instead of a local extractor.

Rules pinned here: the routing rule (what goes where), the pre-flight refusals with zero
spend, the provider reference cached in the sidecar (reused until it expires, re-uploaded
when the key rotates), the measured token count that feeds the TPM gate, and the fact that
one file is one request — never split. No network (b01): the FakeRouter stands in for the
Files API and records what it was asked to do."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import FakeCtx, FakeRouter, make_app_cfg, make_deps
from gemini_mcp import ingest
from gemini_mcp.config import IngestCfg
from gemini_mcp.progress import ProgressEmitter
from gemini_mcp.tools import analyze_file_tool, file_upload_ticket_tool

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
TOKEN = "tok123"


def _deps(tmp_path, monkeypatch, router=None, **ingest_kw):
    monkeypatch.setenv("GEMINI_MCP_TOKEN", TOKEN)
    cfg = make_app_cfg(tmp_path / "logs").model_copy(
        update={"ingest": IngestCfg(dir=str(tmp_path / "inbox"), use_pandoc=False,
                                    **ingest_kw)})
    return make_deps(router=router or FakeRouter(), app_cfg=cfg)


async def _uploaded(deps, filename: str, data: bytes) -> str:
    ticket = await file_upload_ticket_tool(deps, filename, len(data))
    ingest.save_upload(Path(deps.app_cfg.ingest.dir), ticket["upload_id"], data,
                       50 * 2**20)
    return ticket["upload_id"]


async def _analyze(deps, upload_id: str, **params):
    return await analyze_file_tool(deps, ProgressEmitter(FakeCtx()),
                                  {"task": "summarize", "upload_id": upload_id,
                                   **params})


# ------------------------------------------------------------------ detection
@pytest.mark.parametrize(("name", "data", "mime"), [
    ("a.pdf", PDF_BYTES, "application/pdf"),
    ("a.png", PNG_BYTES, "image/png"),
    ("a.wav", WAV_BYTES, "audio/wav"),
    ("a.m4a", b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 16, "audio/mp4"),
    ("a.mp4", b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16, "video/mp4"),
    ("a.ogg", b"OggS\x00\x02" + b"\x00" * 16, "audio/ogg"),
])
def test_media_kind_by_content(name, data, mime):
    assert ingest.media_kind(data, name) is not None
    assert ingest.media_kind(data, name)[1] == mime


def test_text_named_like_media_stays_text():
    """A .mp4 full of plain text is text: extension is only a hint for binary bytes."""
    assert ingest.media_kind(b"just words, no media here", "notes.mp4") is None
    assert ingest.media_kind(b"PK\x03\x04zip", "doc.docx") is None   # pandoc's, not the API's


# ------------------------------------------------------------------ the provider path
async def test_pdf_runs_one_pinned_measured_request(tmp_path, monkeypatch):
    router = FakeRouter(file_tokens=1234)
    deps = _deps(tmp_path, monkeypatch, router=router)
    up_id = await _uploaded(deps, "report.pdf", PDF_BYTES)
    out = await _analyze(deps, up_id)

    assert len(router.uploads) == 1                       # one file, one upload
    assert router.uploads[0]["mime"] == "application/pdf"
    assert len(router.calls) == 1                         # one file, ONE request
    assert router.files_seen[0][0].key_id                 # pinned to the uploading key
    req = router.calls[0]
    # the TPM gate is fed the measured file tokens, not a text estimate
    assert req.est_tokens >= 1234
    assert req.prompt == "summarize"

    f = out["meta"]["file"]
    assert f["via"] == "files_api" and f["mime"] == "application/pdf"
    assert f["tokens"] == 1234 and f["bytes"] == len(PDF_BYTES)
    assert f["provider"]["uri"].startswith("https://provider.test/")
    assert out["meta"]["requests_spent"] == 1
    assert "via Files API" in out["report"] and "1234 tokens" in out["report"]
    stages = [e["stage"] for e in out["meta"]["progress_trace"]]
    assert "uploading" in stages and "uploaded" in stages
    uploaded = next(e for e in out["meta"]["progress_trace"] if e["stage"] == "uploaded")
    assert "files/f1" in uploaded["message"] and "1234 tokens" in uploaded["message"]


async def test_image_and_audio_take_the_same_path(tmp_path, monkeypatch):
    router = FakeRouter()
    deps = _deps(tmp_path, monkeypatch, router=router)
    for name, data in (("shot.png", PNG_BYTES), ("clip.wav", WAV_BYTES)):
        up_id = await _uploaded(deps, name, data)
        out = await _analyze(deps, up_id)
        assert out["meta"]["file"]["via"] == "files_api"
    assert [u["mime"] for u in router.uploads] == ["image/png", "audio/wav"]


async def test_text_documents_keep_the_old_pipeline(tmp_path, monkeypatch):
    """The routing rule: what can be extracted as text is chunked, cached and cheap."""
    router = FakeRouter()
    deps = _deps(tmp_path, monkeypatch, router=router)
    body = "b" * 4000                        # over the test threshold → chunked
    up_id = await _uploaded(deps, "notes.txt", body.encode())
    out = await _analyze(deps, up_id)
    assert out["meta"]["file"]["via"] == "text"
    assert not router.uploads                            # never went near the API
    assert len(router.calls) > 1                         # chunked, as before


# ------------------------------------------------------------------ pre-flight, zero spend
async def test_pdf_over_the_page_ceiling_is_refused_before_any_upload(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "pdfinfo"
    stub.write_text("#!/bin/sh\necho 'Pages:          1501'\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    router = FakeRouter()
    deps = _deps(tmp_path, monkeypatch, router=router)
    up_id = await _uploaded(deps, "huge.pdf", PDF_BYTES)
    with pytest.raises(ToolError) as ei:
        await _analyze(deps, up_id)
    payload = json.loads(str(ei.value))
    assert payload["error"] == "pdf_too_many_pages" and payload["pages"] == 1501
    assert "never split" in payload["hint"]
    assert not router.uploads and not router.calls       # nothing left the server


async def test_pdf_over_the_size_ceiling_is_refused_before_any_upload(tmp_path, monkeypatch):
    router = FakeRouter()
    deps = _deps(tmp_path, monkeypatch, router=router, pdf_max_mb=1)
    up_id = await _uploaded(deps, "big.pdf", PDF_BYTES + b"x" * (1_048_576 + 1))
    with pytest.raises(ToolError) as ei:
        await _analyze(deps, up_id)
    assert json.loads(str(ei.value))["error"] == "pdf_too_large"
    assert not router.uploads and not router.calls


async def test_files_api_can_be_switched_off(tmp_path, monkeypatch):
    router = FakeRouter()
    deps = _deps(tmp_path, monkeypatch, router=router, files_api=False)
    up_id = await _uploaded(deps, "report.pdf", PDF_BYTES)
    with pytest.raises(ToolError) as ei:
        await _analyze(deps, up_id)
    payload = json.loads(str(ei.value))
    assert payload["error"] == "files_api_disabled" and payload["mime"] == "application/pdf"
    assert "zero quota spent" in payload["hint"]
    assert not router.uploads and not router.calls


async def test_upload_failure_names_itself_and_spends_nothing(tmp_path, monkeypatch):
    from gemini_router import RouterError

    router = FakeRouter(upload_error=RouterError("provider said no"))
    deps = _deps(tmp_path, monkeypatch, router=router)
    up_id = await _uploaded(deps, "report.pdf", PDF_BYTES)
    with pytest.raises(ToolError) as ei:
        await _analyze(deps, up_id)
    assert json.loads(str(ei.value))["error"] == "file_upload_failed"
    assert not router.calls                              # no model call was made


# ------------------------------------------------------------------ the cached reference
async def test_second_analysis_reuses_the_uploaded_file(tmp_path, monkeypatch):
    """Re-uploading on every call would burn a 20 MB transfer for nothing: the blob for a
    given upload_id cannot change, so the reference is reused until it expires."""
    router = FakeRouter()
    deps = _deps(tmp_path, monkeypatch, router=router)
    up_id = await _uploaded(deps, "report.pdf", PDF_BYTES)
    await _analyze(deps, up_id, request_id=None)
    side = ingest.load_sidecar(Path(deps.app_cfg.ingest.dir), up_id)
    assert side["provider"]["uri"].endswith("/files/f1")
    await _analyze(deps, up_id, request_id=None, task="another question")
    assert len(router.uploads) == 1                      # uploaded once, analyzed twice
    assert len(router.calls) == 2


async def test_expired_reference_is_uploaded_again(tmp_path, monkeypatch):
    import time as _t

    router = FakeRouter(expiry="2020-01-01T00:00:00Z")   # long gone
    deps = _deps(tmp_path, monkeypatch, router=router)
    up_id = await _uploaded(deps, "report.pdf", PDF_BYTES)
    await _analyze(deps, up_id)
    await _analyze(deps, up_id, request_id=None)
    assert len(router.uploads) == 2
    assert _t.time() > 0


async def test_rotated_key_drops_the_cached_reference(tmp_path, monkeypatch):
    """Files belong to a key's project: a reference from a key no longer configured is
    invisible, so the blob is uploaded again under a live one."""
    router = FakeRouter()
    deps = _deps(tmp_path, monkeypatch, router=router)
    up_id = await _uploaded(deps, "report.pdf", PDF_BYTES)
    await _analyze(deps, up_id)
    side = ingest.load_sidecar(Path(deps.app_cfg.ingest.dir), up_id)
    side["provider"]["key_id"] = "deadbeef"              # that key is gone now
    ingest.sidecar_path(Path(deps.app_cfg.ingest.dir), up_id).write_text(
        json.dumps(side), encoding="utf-8")
    await _analyze(deps, up_id, request_id=None)
    assert len(router.uploads) == 2


async def test_missing_pdfinfo_never_blocks_a_pdf(tmp_path, monkeypatch):
    """pdfinfo is a pre-flight nicety: without it the provider stays the authority."""
    monkeypatch.setenv("PATH", str(tmp_path))            # no tools at all
    assert shutil.which("pdfinfo") is None
    router = FakeRouter()
    deps = _deps(tmp_path, monkeypatch, router=router)
    up_id = await _uploaded(deps, "report.pdf", PDF_BYTES)
    out = await _analyze(deps, up_id)
    assert out["meta"]["file"]["via"] == "files_api"


# ------------------------------------------------------------------ the report line
def test_routing_noise_is_not_a_rough_edge():
    """The `routing` event's message embeds its detail dict, and that dict has a key named
    `wait` — so a plain text scan flagged EVERY live request as rough and made the signal
    worthless. Stages are the structured truth; the probe that found this ran against the
    production daemon (worklog report, 2026-10-11)."""
    from gemini_mcp.tools import _report

    clean = {"backend": "router", "route": "optimal", "model": "gemini-3.6-flash",
             "requests_spent": 1, "elapsed_s": 3.3,
             "progress_trace": [
                 {"stage": "uploaded", "message": "provider file files/x ready · 560 tokens"},
                 {"stage": "router:routing",
                  "message": "? {'est_tokens': 585, 'wait': True, 'overflow': 'lite+gemma'}"},
                 {"stage": "router:dispatch", "message": "dispatch → gemini-3.6-flash"},
                 {"stage": "router:ok", "message": "ok ← gemini-3.6-flash in=541 out=31"}]}
    assert "clean run" in _report(clean) and "rough_edges" not in _report(clean)

    rough = {**clean, "progress_trace": clean["progress_trace"] + [
        {"stage": "router:waiting", "message": "waiting 8.4s for gemini-3.8-flash RPM window"}]}
    assert "rough_edges=1" in _report(rough)

    switched = {**clean, "progress_trace": clean["progress_trace"] + [
        {"stage": "router:switch", "message": "switch (reason: unavailable)"}]}
    assert "rough_edges=1" in _report(switched)
