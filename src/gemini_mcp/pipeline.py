"""Massive-payload pipeline (FR-D): measure → plan → budget pre-flight (zero-spend
fail-fast) → chunk loop with 'CHUNK N/M' markers → recursive consolidation → compact
result + meta. All spends go through the router (shared ledger)."""
from __future__ import annotations

import time
from dataclasses import dataclass

from gemini_router import QuotaExhaustedError, Router, RouterError, RouterRequest, RouterResult

from .chunker import plan_chunks, split_text
from .config import AppCfg
from .progress import ProgressEmitter
from .prompts import chunk_prompt, consolidation_prompt


class PlanBudgetError(QuotaExhaustedError):
    """Chunk plan does not fit the remaining budget — raised BEFORE any spend (FR-D4)."""

    def __init__(self, needed: int, available: int, per_model: list[dict], hint: str,
                 reset_eta_pt: str = ""):
        super().__init__(per_model, reset_eta_pt, hint=hint)
        self.needed = needed
        self.available = available

    def to_dict(self) -> dict:
        return {"error": "plan_budget_insufficient", "requests_needed": self.needed,
                "requests_available": self.available, "per_model": self.per_model,
                "hint": self.hint, "reset_eta_pt": self.reset_eta_pt}


@dataclass(slots=True)
class AnalyzeParams:
    task: str
    body: str = ""
    thinking_level: str | None = None       # None → app default (high)
    output_format: str = "text"
    max_output_tokens: int | None = None
    model: str | None = None
    overflow: str = "off"
    wait: bool | None = None
    chunk_tokens: int | None = None
    overlap_ratio: float | None = None
    request_id: str | None = None
    result_max_chars: int | None = None
    source: str = "mcp"
    route: str = "optimal"                  # router already selected upstream (tools)


class Pipeline:
    def __init__(self, router: Router, cfg: AppCfg, emitter: ProgressEmitter, logger=None):
        self.router = router
        self.cfg = cfg
        self.em = emitter
        self.log = logger
        self._spends = 0
        self._cache_hits = 0
        self._models: list[str] = []

    def _account(self, res: RouterResult) -> None:
        if res.cached:
            self._cache_hits += 1
        else:
            self._spends += 1
        self._models.append(res.model)

    def _rid(self, p: AnalyzeParams, suffix: str) -> str | None:
        return f"{p.request_id}:{suffix}" if p.request_id else None

    async def analyze(self, p: AnalyzeParams) -> tuple[str, dict]:
        cfgp = self.cfg.pipeline
        thinking = p.thinking_level or self.cfg.thinking.default
        t0 = time.time()
        body = p.body or ""
        await self.em.stage("received", 2,
                            f"received: task {len(p.task)} chars, body {len(body)} chars, "
                            f"thinking={thinking}, overflow={p.overflow}",
                            task_chars=len(p.task), body_chars=len(body))
        on_ev = self.em.router_event_sync
        chunks_meta: dict = {"count": 0}
        measured = 0

        if not body.strip():
            res = await self._complete(p, RouterRequest(
                prompt=p.task, thinking_level=thinking, output_format=p.output_format,
                max_output_tokens=p.max_output_tokens, model=p.model, overflow=p.overflow,
                wait=p.wait, request_id=p.request_id, source=p.source, kind="single"), on_ev)
            text = res.text
            measured = res.tokens_in or 0
        else:
            measured = await self.router.count_tokens(p.task + "\n\n" + body)
            await self.em.stage("measured", 5, f"measured: {measured} tokens total",
                                tokens=measured)
            chunk_tokens = p.chunk_tokens or cfgp.chunk_size_tokens
            if measured <= max(cfgp.chunk_threshold_tokens, chunk_tokens):
                res = await self._complete(p, RouterRequest(
                    prompt=body, system=p.task, thinking_level=thinking,
                    output_format=p.output_format, max_output_tokens=p.max_output_tokens,
                    model=p.model, overflow=p.overflow, wait=p.wait,
                    request_id=p.request_id, source=p.source, kind="single",
                    est_tokens=measured), on_ev)
                text = res.text
            else:
                text, chunks_meta = await self._chunked(p, body, measured, chunk_tokens,
                                                        thinking, on_ev)

        limit = p.result_max_chars or cfgp.max_result_chars
        truncated = len(text) > limit
        if truncated:
            text = text[:limit]
        await self.em.stage("done", 100,
                            f"done in {time.time() - t0:.1f}s: spent {self._spends} "
                            f"request(s), {self._cache_hits} cache hit(s)",
                            spent=self._spends, cache_hits=self._cache_hits)
        await self.em.drain()
        meta = {
            "request_id": p.request_id, "models_used": self._models,
            "thinking_level": thinking, "output_format": p.output_format,
            "measured_tokens": measured, "chunks": chunks_meta,
            "requests_spent": self._spends, "cache_hits": self._cache_hits,
            "quota_after": self.router.model_budgets(),
            "elapsed_s": round(time.time() - t0, 1), "truncated": truncated,
            "progress_trace": self.em.trace,
        }
        return text, meta

    async def _complete(self, p: AnalyzeParams, req: RouterRequest, on_ev) -> RouterResult:
        res = await self.router.complete(req, on_event=on_ev)
        self._account(res)
        if req.kind != "count" and not (res.text or "").strip():
            # v0.1.1 live-smoke regression: 3.x thinking tokens count toward
            # max_output_tokens — starvation returns HTTP-ok with empty text.
            raise RouterError(
                f"empty answer from {res.model} "
                f"(finish={getattr(res, 'finish_reason', None)}): thinking tokens likely "
                "consumed max_output_tokens — raise it and retry")
        return res

    async def _chunked(self, p: AnalyzeParams, body: str, measured: int,
                       chunk_tokens: int, thinking: str | None, on_ev) -> tuple[str, dict]:
        cfgp = self.cfg.pipeline
        ratio = p.overlap_ratio if p.overlap_ratio is not None else cfgp.overlap_ratio
        ratio = min(max(ratio, 0.0), 0.5)
        cpt = max(1.0, len(body) / max(1, measured))            # real chars/token
        max_chars = max(1, int(chunk_tokens * cpt))
        overlap_chars = int(max_chars * ratio)
        chunks = split_text(body, max_chars, overlap_chars, cfgp.merge_tail_ratio)
        m = len(chunks)
        planned = plan_chunks(measured, chunk_tokens, ratio)
        cost = m + 1
        await self.em.stage("plan", 7,
                            f"plan: {m} chunks (~{planned} estimated), overlap {ratio:.0%}, "
                            f"cost ≈ {cost} requests", chunks=m, planned=planned, cost=cost)

        available = self._available_rpd(p)
        if available < cost:
            budgets = self.router.model_budgets()
            raise PlanBudgetError(
                needed=cost, available=available, per_model=budgets,
                hint=(f"body needs {cost} requests ({m} chunks + consolidation) but only "
                      f"{available} RPD left; shrink the body, raise chunk_tokens, retry "
                      f"after reset, or pass overflow='lite'/'lite+gemma'"))
        await self.em.stage("budget_checked", 9,
                            f"budget ok: need {cost}, available {available} RPD",
                            needed=cost, available=available)

        answers: dict[int, str] = {}
        failed: list[int] = []
        for i, ch in enumerate(chunks, 1):
            await self.em.stage("chunk", 10 + 80 * (i - 1) / m,
                                f"chunk {i}/{m}: dispatching ({len(ch)} chars)", i=i, m=m)
            req = RouterRequest(
                prompt=chunk_prompt(p.task, i, m, ch), thinking_level=thinking,
                output_format="text", max_output_tokens=p.max_output_tokens,
                model=p.model, overflow=p.overflow, wait=p.wait,
                request_id=self._rid(p, f"c{i}"), source=p.source, kind="chunk",
                est_tokens=max(1, int(len(ch) / cpt)))
            try:
                res = await self._complete(p, req, on_ev)
                answers[i] = res.text
                await self.em.stage("chunk_done", 10 + 80 * i / m,
                                    f"chunk {i}/{m} analyzed on {res.model} "
                                    f"({res.latency_s:.1f}s{', cached' if res.cached else ''})",
                                    i=i, m=m, model=res.model, cached=res.cached)
            except QuotaExhaustedError:
                raise                                            # quota death: abort, FR-B8
            except RouterError as e:
                if cfgp.fail_on_chunk_error:
                    raise
                failed.append(i)
                await self.em.stage("chunk_failed", 10 + 80 * i / m,
                                    f"chunk {i}/{m} FAILED ({e}) — continuing with gap",
                                    i=i, m=m, error=str(e))

        text, rounds = await self._consolidate(p, answers, failed, m, thinking, cpt,
                                               chunk_tokens, on_ev)
        chunks_meta = {"count": m, "tokens_in": measured, "overlap_ratio": ratio,
                       "failed": failed, "consolidation_rounds": rounds}
        return text, chunks_meta

    def _available_rpd(self, p: AnalyzeParams) -> int:
        available = sum(b["rpd_left"] for b in self.router.model_budgets())
        if p.overflow in ("lite", "lite+gemma"):
            tiers = [t for t in getattr(self.router.cfg, "overflow_tiers", [])
                     if t != "gemma-auto"]
            if tiers:
                available += sum(b["rpd_left"] for b in self.router.model_budgets(tiers))
            if p.overflow == "lite+gemma":
                limits = getattr(self.router.cfg, "overflow_limits", None)
                available += getattr(limits, "rpd", 500)         # gemma-auto is unpinned
        return available

    async def _consolidate(self, p: AnalyzeParams, answers: dict[int, str],
                           failed: list[int], m: int, thinking: str | None,
                           cpt: float, chunk_tokens: int, on_ev) -> tuple[str, int]:
        cfgp = self.cfg.pipeline
        parts = [(i, answers[i]) for i in sorted(answers)]
        if not parts:
            raise PlanBudgetError(needed=0, available=0, per_model=[],
                                  hint=f"all {m} chunks failed: {failed}")
        round_no = 1
        while True:
            prompt = consolidation_prompt(p.task, parts, m, failed, round_no)
            est = max(1, int(len(prompt) / max(1.0, cpt)))
            if est <= chunk_tokens or len(parts) == 1:
                await self.em.stage("consolidating", 92,
                                    f"consolidation round {round_no}: final "
                                    f"({len(parts)} input(s), ~{est} tok)",
                                    round=round_no, final=True)
                res = await self._complete(p, RouterRequest(
                    prompt=prompt, thinking_level=thinking, output_format=p.output_format,
                    max_output_tokens=p.max_output_tokens, model=p.model,
                    overflow=p.overflow, wait=p.wait,
                    request_id=self._rid(p, f"cons{round_no}"), source=p.source,
                    kind="consolidate", est_tokens=est), on_ev)
                return res.text, round_no
            fanin = max(2, cfgp.consolidation_fanin)
            groups = [parts[j:j + fanin] for j in range(0, len(parts), fanin)]
            await self.em.stage("consolidating", min(91, 88 + round_no),
                                f"consolidation round {round_no}: too big (~{est} tok) — "
                                f"reducing {len(parts)} → {len(groups)} groups (fanin {fanin})",
                                round=round_no, final=False, groups=len(groups))
            merged: list[tuple[int, str]] = []
            for gi, group in enumerate(groups, 1):
                res = await self._complete(p, RouterRequest(
                    prompt=consolidation_prompt(p.task, group, m, failed, round_no,
                                                final=False),
                    thinking_level=thinking, output_format="text",
                    max_output_tokens=p.max_output_tokens, model=p.model,
                    overflow=p.overflow, wait=p.wait,
                    request_id=self._rid(p, f"cons{round_no}g{gi}"), source=p.source,
                    kind="consolidate"), on_ev)
                merged.append((group[0][0], res.text))
            parts = merged
            round_no += 1
