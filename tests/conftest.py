"""Fakes & fixtures. No network, no real keys (b01)."""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace

from gemini_router import QuotaExhaustedError, RouterError, RouterEvent, RouterResult
from gemini_router.config import Config as RouterConfig

from gemini_mcp.config import AppCfg
from gemini_mcp.tools import Deps


class FakeRouter:
    """countTokens = len//4; response text depends on request kind."""

    def __init__(self, *, budgets=None, fail_chunks=(), raise_quota=False, cfg=None):
        self.calls: list = []
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
            quota_day_tz="America/Los_Angeles")
        self.ledger = SimpleNamespace(calls_tail=lambda *a, **k: [])

    async def count_tokens(self, prompt: str, model=None) -> int:
        self.count_calls.append(prompt)
        return max(1, len(prompt) // 4)

    async def complete(self, req, on_event=None) -> RouterResult:
        if self.raise_quota:
            raise QuotaExhaustedError(self.budgets, "2026-10-09T07:00:00+00:00",
                                      hint="test")
        self.calls.append(req)
        if on_event:
            on_event(RouterEvent(stage="dispatch", model="gemini-3.8-flash",
                                 key_id="fake", detail={"est_tokens": req.est_tokens or 0}))
        if req.kind == "chunk":
            mm = re.search(r"CHUNK (\d+)/(\d+)", req.prompt)
            if mm and int(mm.group(1)) in self.fail_chunks:
                raise RouterError("simulated chunk failure")
        if req.kind == "chunk":
            text = f"analysis-of-chunk-{mm.group(1)}-of-{mm.group(2)}"
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
            "consolidation_fanin": 2, "max_result_chars": 12000,
            "fail_on_chunk_error": True}
    pipe.update(pipeline_over)
    return AppCfg.model_validate({"pipeline": pipe, "logging": {"dir": str(log_dir)}})


def make_deps(router=None, app_cfg=None) -> Deps:
    return Deps(router=router or FakeRouter(),
                app_cfg=app_cfg or make_app_cfg(),
                audit=NullAudit(), logger=logging.getLogger("test"))


def real_router_cfg(tmp_path, **over) -> RouterConfig:
    data = {"api_keys": ["k1"], "state_path": str(tmp_path / "state.db"),
            "chain": ["gemini-3.8-flash", "gemini-3.7-flash"]}
    data.update(over)
    return RouterConfig.model_validate(data)


def big_body(n_sentences: int = 200, filler: int = 20) -> str:
    return "".join(f"Sentence {i} " + "x" * filler + ". " for i in range(n_sentences))
