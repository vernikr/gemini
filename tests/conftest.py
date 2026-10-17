"""Fakes & fixtures. No network, no real keys (b01)."""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace

from gemini_router import (
    Ledger,
    QuotaExhaustedError,
    RouterError,
    RouterEvent,
    RouterResult,
    UploadedFile,
    key_id,
)
from gemini_router.config import Config as RouterConfig

from gemini_mcp.config import AppCfg
from gemini_mcp.tools import Deps


class FakeRouter:
    """countTokens = len//4; response text depends on request kind. The Files API half
    (`upload_file`, `count_file_tokens`, `files=` on complete) mirrors the real one's
    contract: the returned record carries the key_id the request gets pinned to."""

    KEY = "fake-key"
    EXPIRY = "2099-01-01T00:00:00Z"          # never stale in tests that do not say so

    def __init__(self, *, budgets=None, fail_chunks=(), raise_quota=False, cfg=None,
                 file_tokens=1089, upload_error=None, expiry=None):   # = image forecast: clean
        self.calls: list = []
        self.uploads: list[dict] = []
        self.file_counts: list[tuple] = []
        self.files_seen: list[list] = []
        self.file_paths_seen: list[list] = []
        self.file_count_levels: list[str | None] = []
        self.file_tokens = file_tokens
        self.upload_error = upload_error
        self.expiry = expiry or self.EXPIRY
        self.count_calls: list[str] = []
        self.fail_chunks = set(fail_chunks)
        self.empty_chunks: set[int] = set()   # starvation sim: ok result, empty text
        self.raise_quota = raise_quota
        self.budgets = budgets if budgets is not None else [
            {"model": "gemini-3.8-flash", "rpd_left": 100, "rpm_wait_s": 0,
             "tpm_wait_s": 0, "cooldown_until": None, "best_wait_s": 0}]
        self.cfg = cfg or SimpleNamespace(
            overflow_tiers=["gemini-3.5-flash-lite", "gemma-auto"],
            overflow_limits=SimpleNamespace(rpd=500),
            limits_per_key_model=SimpleNamespace(rpd=20, rpm=5, tpm=250000),
            quota_day_tz="America/Los_Angeles",
            window_s=60.0, planned_ttl_s=300.0,
            chain=["gemini-3.8-flash", "gemini-3-flash-preview"],
            api_keys=[self.KEY])
        # a real in-memory ledger: router_logs and the drift record path run against the
        # genuine surface, which is exactly why the fake used to miss drift-shaped rows
        self.ledger = Ledger(":memory:")

    async def count_tokens(self, prompt: str, model=None) -> int:
        self.count_calls.append(prompt)
        return max(1, len(prompt) // 4)

    # ------------------------------------------------------------ Files API
    async def upload_file(self, path, *, mime=None, model=None, source="cli",
                          display_name=None, key=None, overflow="off", sha256=None,
                          ready_timeout_s=30.0) -> UploadedFile:
        if self.upload_error:
            raise self.upload_error
        from pathlib import Path as _P

        self.uploads.append({"path": str(path), "mime": mime, "model": model,
                             "source": source, "display_name": display_name,
                             "overflow": overflow})
        n = len(self.uploads)
        return UploadedFile(name=f"files/f{n}", uri=f"https://provider.test/files/f{n}",
                            mime=mime or "application/octet-stream",
                            size=_P(path).stat().st_size, state="ACTIVE",
                            expires_at=self.expiry, sha256=sha256 or "a" * 64,
                            key_id=key_id(self.KEY))

    async def count_file_tokens(self, model, uri, mime, *, key_id=None,
                                media_resolution=None) -> int:
        self.file_counts.append((model, uri, mime))
        self.file_count_levels.append(media_resolution)
        return self.file_tokens

    async def complete(self, req, on_event=None, files=None, file_paths=None) -> RouterResult:
        if self.raise_quota:
            raise QuotaExhaustedError(self.budgets, "2026-10-09T07:00:00+00:00",
                                      hint="test")
        self.calls.append(req)
        self.files_seen.append(list(files or []))
        self.file_paths_seen.append(list(file_paths or []))
        if file_paths:
            from pathlib import Path as _P
            assert len(file_paths) == len(files or [])
            assert all(_P(p).is_file() for p in file_paths)  # usable for key re-upload
        if on_event:
            on_event(RouterEvent(stage="dispatch", model="gemini-3.8-flash",
                                 key_id="fake", detail={"est_tokens": req.est_tokens or 0}))
        if req.kind == "chunk":
            mm = re.search(r"(?:CHUNK|GROUP|DOCUMENT PART) (\d+)/(\d+)", req.prompt)
            if mm and int(mm.group(1)) in self.fail_chunks:
                raise RouterError("simulated chunk failure")
        if req.kind == "chunk":
            label = ("group" if "GROUP" in req.prompt else
                     "pdf" if "DOCUMENT PART" in req.prompt else "chunk")
            text = f"analysis-of-{label}-{mm.group(1)}-of-{mm.group(2)}"
            if mm and int(mm.group(1)) in self.empty_chunks:
                text = ""                     # thinking-starved answer (router v0.1.1)
        elif req.kind == "consolidate":
            text = "CONSOLIDATED[" + str(req.prompt.count("--- ANALYSIS OF PART")) + "]"
        else:
            text = "SINGLE[" + (req.system or req.prompt)[:12] + "]"
        if on_event:
            on_event(RouterEvent(stage="ok", model="gemini-3.8-flash", key_id="fake",
                                 detail={"tokens_in": 5, "tokens_out": 3,
                                         "latency_s": 0.01}))
        return RouterResult(text=text, model="gemini-3.8-flash", key_id="fake",
                            tokens_in=max(1, len(req.prompt) // 4), tokens_out=6,
                            latency_s=0.01, cached=False, request_id=req.request_id,
                            quota_after=self.budgets,
                            finish_reason="STOP" if text else "MAX_TOKENS")

    def model_budgets(self, models=None, now=None):
        return list(self.budgets)

    def quota_table(self, now=None):
        return [{"key_id": "fake", "model": b["model"], "rpd_left": b["rpd_left"],
                 "rpd_limit": 20, "rpm_used": 0, "rpm_limit": 5, "tpm_used": 0,
                 "tpm_limit": 250000, "rpm_wait_s": 0, "tpm_wait_s": 0,
                 "cooldown_until": None, "wait_s": 0, "reason": None,
                 "recommended": i == 0} for i, b in enumerate(self.budgets)]


class FakeCtx:
    def __init__(self):
        self.progress: list[tuple] = []
        self.messages: list[str] = []

    async def report_progress(self, progress, total=None, message=None):
        self.progress.append((progress, total, message))

    async def info(self, data, **kw):
        self.messages.append(str(data))


class NullAudit:
    def __init__(self):
        self.entries: list[dict] = []

    def record(self, entry: dict):
        self.entries.append(entry)


def make_app_cfg(log_dir="logs", **pipeline_over) -> AppCfg:
    pipe = {"chunk_threshold_tokens": 100, "chunk_size_tokens": 60, "overlap_ratio": 0.2,
            "consolidation_fanin": 2,
            "fail_on_chunk_error": True}
    pipe.update(pipeline_over)
    return AppCfg.model_validate({"pipeline": pipe, "logging": {"dir": str(log_dir)}})


def make_deps(router=None, app_cfg=None, backend: str = "router") -> Deps:
    return Deps(router=router or FakeRouter(),
                app_cfg=app_cfg or make_app_cfg(),
                audit=NullAudit(), logger=logging.getLogger("test"),
                backend=backend)


def real_router_cfg(tmp_path, **over) -> RouterConfig:
    data = {"api_keys": ["k1"], "state_path": str(tmp_path / "state.db"),
            "chain": ["gemini-3.8-flash", "gemini-3.7-flash"]}
    data.update(over)
    return RouterConfig.model_validate(data)


def big_body(n_sentences: int = 200, filler: int = 20) -> str:
    return "".join(f"Sentence {i} " + "x" * filler + ". " for i in range(n_sentences))
