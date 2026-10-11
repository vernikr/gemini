"""C2: several files, one answer — packing by measured tokens (wave 2).

Rules pinned here: binaries are measured for free then packed first-fit-decreasing
against the serving key's FULL minute budget; the packer never splits a file (the
oversized-PDF branch is covered separately in test_pdf_split); text documents keep their own
pipeline; the parts consolidate into one result. No network (b01)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import FakeCtx, FakeRouter, make_app_cfg, make_deps
from gemini_mcp import ingest
from gemini_mcp.config import IngestCfg
from gemini_mcp.progress import ProgressEmitter
from gemini_mcp.tools import _pack_bins, analyze_files_tool, file_upload_ticket_tool

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _cfg(tpm: int):
    return SimpleNamespace(
        overflow_tiers=["gemini-3.5-flash-lite", "gemma-auto"],
        overflow_limits=SimpleNamespace(rpd=500),
        limits_per_key_model=SimpleNamespace(rpd=20, rpm=5, tpm=tpm),
        quota_day_tz="America/Los_Angeles", window_s=60.0, planned_ttl_s=300.0,
        chain=["gemini-3.8-flash", "gemini-3-flash-preview"],
        api_keys=[FakeRouter.KEY])


def _deps(tmp_path, monkeypatch, router):
    monkeypatch.setenv("GEMINI_MCP_TOKEN", "tok123")
    # big chunk_size: the group consolidation must land in ONE final round here, so the
    # fake's CONSOLIDATED[n] counts parts, not fan-in intermediates
    cfg = make_app_cfg(tmp_path / "logs", chunk_size_tokens=100000).model_copy(
        update={"ingest": IngestCfg(dir=str(tmp_path / "inbox"), use_pandoc=False)})
    return make_deps(router=router, app_cfg=cfg)


async def _upload(deps, filename: str, data: bytes) -> str:
    ticket = await file_upload_ticket_tool(deps, filename, len(data))
    ingest.save_upload(Path(deps.app_cfg.ingest.dir), ticket["upload_id"], data,
                       50 * 2**20)
    return ticket["upload_id"]


async def _analyze(deps, upload_ids, **params):
    return await analyze_files_tool(deps, ProgressEmitter(FakeCtx()),
                                    {"task": "summarize", "upload_ids": upload_ids,
                                     **params})


# ------------------------------------------------------------------ the packer itself
def test_pack_bins_first_fit_decreasing_never_splits_a_file():
    items = [{"filename": f"f{i}", "tokens": t} for i, t in enumerate([60, 40, 30, 95])]
    packs, refused = _pack_bins(items, budget=100, overhead=10)
    assert [it["tokens"] for it in refused] == [95]              # 95+10 > 100: refused
    assert [[it["tokens"] for it in p] for p in packs] == [[60], [40, 30]]


async def test_two_images_fit_one_pack_and_skip_consolidation(tmp_path, monkeypatch):
    router = FakeRouter(file_tokens=2000)
    deps = _deps(tmp_path, monkeypatch, router)
    ids = [await _upload(deps, f"shot{i}.png", PNG_BYTES) for i in (1, 2)]
    out = await _analyze(deps, ids)

    assert len(router.uploads) == 2
    assert len(router.calls) == 1                                 # one pack, no consolidate
    assert router.calls[0].kind == "chunk"
    assert len(router.files_seen[0]) == 2                         # both files in ONE request
    assert out["result"] == "analysis-of-group-1-of-1"
    assert out["meta"]["groups"] == [{"group": 1, "files": ["shot1.png", "shot2.png"],
                                      "tokens": 4000, "model": "gemini-3.8-flash"}]
    assert out["meta"]["consolidation_rounds"] == 0
    assert [f["group"] for f in out["meta"]["files"]] == [1, 1]
    # the measured cost missed the forecast → the wave-1 drift signal rides along
    assert "COST DRIFT" in out["report"]
    assert len(router.ledger.drift_recent()) == 2


async def test_small_budget_forces_two_packs_and_consolidation(tmp_path, monkeypatch):
    # budget 2500, per-file need 1089+66 → two fit, the third opens pack 2
    router = FakeRouter(cfg=_cfg(2500))
    deps = _deps(tmp_path, monkeypatch, router)
    ids = [await _upload(deps, f"img{i}.png", PNG_BYTES) for i in (1, 2, 3)]
    out = await _analyze(deps, ids)

    kinds = [c.kind for c in router.calls]
    assert kinds == ["chunk", "chunk", "consolidate"]
    assert [len(fs) for fs in router.files_seen[:2]] == [2, 1]
    assert out["result"] == "CONSOLIDATED[2]"
    assert out["meta"]["consolidation_rounds"] == 1
    assert [f["group"] for f in out["meta"]["files"]] == [1, 1, 2]


async def test_a_file_alone_over_budget_is_refused_with_its_measured_cost(
        tmp_path, monkeypatch):
    router = FakeRouter(cfg=_cfg(2500), file_tokens=3000)
    deps = _deps(tmp_path, monkeypatch, router)
    ids = [await _upload(deps, f"big{i}.png", PNG_BYTES) for i in (1, 2)]
    with pytest.raises(ToolError) as e:
        await _analyze(deps, ids)
    payload = json.loads(str(e.value))
    assert payload["error"] == "group_file_over_budget"
    assert [r["measured_tokens"] for r in payload["refused"]] == [3000, 3000]
    assert router.calls == []                                     # zero generate spend


async def test_text_and_binary_files_consolidate_together(tmp_path, monkeypatch):
    router = FakeRouter()
    deps = _deps(tmp_path, monkeypatch, router)
    text_id = await _upload(deps, "notes.txt", ("b" * 4000).encode())
    png_id = await _upload(deps, "shot.png", PNG_BYTES)
    out = await _analyze(deps, [text_id, png_id])

    vias = {f["filename"]: f["via"] for f in out["meta"]["files"]}
    assert vias == {"notes.txt": "text", "shot.png": "files_api"}
    assert out["result"] == "CONSOLIDATED[2]"                     # text part + binary part
    assert out["meta"]["parts"] == 2
    assert router.uploads and len(router.uploads) == 1            # only the png uploaded


async def test_one_upload_id_delegates_to_the_single_file_contract(tmp_path, monkeypatch):
    router = FakeRouter()
    deps = _deps(tmp_path, monkeypatch, router)
    uid = await _upload(deps, "shot.png", PNG_BYTES)
    out = await _analyze(deps, [uid])
    assert out["meta"]["file"]["via"] == "files_api"              # C1 shape, unchanged
    assert "groups" not in out["meta"]
