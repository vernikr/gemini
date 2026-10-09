"""Dual-mode backends (0.8.0). 'router' = fleet mode: shared SQLite ledger (VPS daemon +
SSH stdio — the stage-1 design, quota truth shared by every client). 'direct' = plain
Google AI Studio: the SAME Router engine, REST protocol and error vocabulary, with a
LOCAL persistent ledger under the config home (~/.config/gemini-mcp/state.db, 0.8.1) —
no server, counters survive restarts and are shared by all gateway processes on this
machine. MemLedger (in-memory, zero side effects) remains for live key probes.

Mode: `backend: auto|direct|router` in config.yaml or GEMINI_MCP_BACKEND. auto → 'router'
only when persistent state exists (GEMINI_ROUTER_CONFIG set or state_path file present),
else 'direct' — the consumer default with zero setup beyond API keys."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from gemini_router import Router
from gemini_router.config import Config as RouterConfig
from gemini_router.ledger import RPD_STATUSES, iso

from .config import AppCfg


class MemLedger:
    """Drop-in in-memory replacement for gemini_router.Ledger (same method contracts,
    same row shapes). Volatile by design: counters/cache/audit die with the process."""

    def __init__(self, max_rows: int = 5_000):
        self.path = ""                          # enforce_audit_size compat: never on disk
        self._lock = threading.Lock()
        self._calls: dict[int, dict] = {}
        self._next_id = 1
        self._max_rows = max_rows
        self._cooldowns: dict[tuple[str, str], tuple[float, str]] = {}
        self._cache: dict[str, dict] = {}

    def close(self) -> None:
        pass

    # ---------------------------------------------------------------- calls
    def plan(self, *, key_id: str, model: str, kind: str, est_tokens: int,
             request_id: str | None, source: str, overflow_tier: int = 0,
             now: float | None = None) -> int:
        now = now or time.time()
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            self._calls[rid] = {
                "id": rid, "ts": now, "ts_iso": iso(now), "request_id": request_id,
                "source": source, "kind": kind, "key_id": key_id, "model": model,
                "status": "planned", "tokens_in": est_tokens, "tokens_out": None,
                "overflow_tier": overflow_tier, "http_status": None, "latency_ms": None,
                "quota_error": None, "retry_after_s": None}
            if len(self._calls) > self._max_rows:      # bounded memory (evict oldest)
                for old in sorted(self._calls, key=lambda i: self._calls[i]["ts"])[:100]:
                    del self._calls[old]
            return rid

    def resolve(self, row_id: int, status: str, *, http_status: int | None = None,
                tokens_in: int | None = None, tokens_out: int | None = None,
                latency_ms: int | None = None, quota_error: str | None = None,
                retry_after_s: float | None = None) -> None:
        with self._lock:
            row = self._calls.get(row_id)
            if not row:
                return
            row["status"] = status
            if http_status is not None:
                row["http_status"] = http_status
            if tokens_in is not None:
                row["tokens_in"] = tokens_in
            row.update(tokens_out=tokens_out, latency_ms=latency_ms,
                       quota_error=(quota_error or "")[:500] or None,
                       retry_after_s=retry_after_s)

    def _match(self, key_id: str, model: str, since: float):
        return [r for r in self._calls.values()
                if r["key_id"] == key_id and r["model"] == model
                and r["kind"] != "count" and r["ts"] >= since]

    def rpd_spent(self, key_id: str, model: str, day_start: float,
                  roll_start: float) -> tuple[int, int]:
        with self._lock:
            rows = [r for r in self._match(key_id, model, min(day_start, roll_start))
                    if r["status"] in RPD_STATUSES]
        return (sum(r["ts"] >= day_start for r in rows),
                sum(r["ts"] >= roll_start for r in rows))

    def window_usage(self, key_id: str, model: str, win_start: float,
                     planned_min: float) -> tuple[int, int, float | None]:
        with self._lock:
            rows = [r for r in self._match(key_id, model, win_start)
                    if r["status"] in RPD_STATUSES
                    or (r["status"] == "planned" and r["ts"] >= planned_min)]
        if not rows:
            return 0, 0, None
        return (len(rows), sum(r["tokens_in"] or 0 for r in rows),
                min(r["ts"] for r in rows))

    def last_call_ts(self, key_id: str, model: str) -> float | None:
        with self._lock:
            rows = self._match(key_id, model, 0.0)
        return max((r["ts"] for r in rows), default=None)

    def calls_tail(self, n: int = 50, *, model: str | None = None, status: str | None = None,
                   source: str | None = None, request_id: str | None = None) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._calls.values()
                    if (not model or r["model"] == model)
                    and (not status or r["status"] == status)
                    and (not source or r["source"] == source)
                    and (not request_id or r["request_id"] == request_id)]
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return rows[:n]

    # ---------------------------------------------------------------- cooldowns
    def cooldown(self, key_id: str, model: str, now: float | None = None) -> float | None:
        now = now or time.time()
        with self._lock:
            self._cooldowns = {k: v for k, v in self._cooldowns.items() if v[0] > now}
            hit = self._cooldowns.get((key_id, model))
        return hit[0] if hit else None

    def set_cooldown(self, key_id: str, model: str, until_ts: float, reason: str) -> None:
        with self._lock:
            k = (key_id, model)
            prev = self._cooldowns.get(k)
            if not prev or until_ts > prev[0]:
                self._cooldowns[k] = (until_ts, reason)

    # ---------------------------------------------------------------- cache
    def cache_get(self, h: str | None, request_id: str | None, ttl_s: float,
                  now: float | None = None) -> dict | None:
        now = now or time.time()
        with self._lock:
            fresh = [r for r in self._cache.values() if r["ts"] >= now - ttl_s]
            if h and request_id:
                hits = [r for r in fresh if r["hash"] == h or r["request_id"] == request_id]
            elif h:
                hits = [r for r in fresh if r["hash"] == h]
            elif request_id:
                hits = [r for r in fresh if r["request_id"] == request_id]
            else:
                return None
        return dict(max(hits, key=lambda r: r["ts"])) if hits else None

    def cache_put(self, h: str, request_id: str | None, model: str, response: str,
                  tokens_in: int | None, tokens_out: int | None,
                  now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            prev = self._cache.get(h)
            self._cache[h] = {"hash": h, "request_id": request_id or
                              (prev or {}).get("request_id"), "model": model, "ts": now,
                              "response": response, "tokens_in": tokens_in,
                              "tokens_out": tokens_out}

    # ---------------------------------------------------------------- maintenance
    def prune(self, *, audit_keep_days: float = 30, cache_ttl_s: float = 24 * 3600,
              now: float | None = None, dry: bool = False) -> dict:
        now = now or time.time()
        cut_calls, cut_cache = now - audit_keep_days * 86400, now - cache_ttl_s
        with self._lock:
            old_calls = [i for i, r in self._calls.items() if r["ts"] < cut_calls]
            old_cache = [h for h, r in self._cache.items() if r["ts"] < cut_cache]
            if not dry:
                for i in old_calls:
                    del self._calls[i]
                for h in old_cache:
                    del self._cache[h]
                self._cooldowns = {k: v for k, v in self._cooldowns.items() if v[0] > now}
        return {"calls_pruned": 0 if dry else len(old_calls),
                "cache_pruned": 0 if dry else len(old_cache),
                "calls_would_prune": len(old_calls), "cache_would_prune": len(old_cache)}

    def stats(self) -> dict:
        with self._lock:
            return {"calls": len(self._calls), "cache": len(self._cache),
                    "size_mb": 0.0, "storage": "memory"}


def resolve_mode(app_cfg: AppCfg, router_cfg: RouterConfig) -> str:
    mode = (os.environ.get("GEMINI_MCP_BACKEND") or app_cfg.backend or "auto").lower()
    if mode not in ("auto", "direct", "router"):
        raise ValueError(f"backend must be auto|direct|router, got {mode!r}")
    if mode == "auto":
        mode = ("router" if (os.environ.get("GEMINI_ROUTER_CONFIG")
                             or Path(router_cfg.state_path).exists()) else "direct")
    return mode


def direct_state_path(router_cfg: RouterConfig) -> str:
    """Where direct mode keeps its local ledger: an explicit state_path (config.yaml
    router section / GEMINI_ROUTER_STATE) wins, else <config home>/state.db — one
    predictable place the wizard manages and `uninstall` removes."""
    from .config import config_home
    return (router_cfg.state_path if router_cfg.state_path != "state.db"
            else str(config_home() / "state.db"))


def make_routers(app_cfg: AppCfg,
                 router_cfg: RouterConfig) -> tuple[Router, Router | None, str]:
    """→ (default_router, optimal_route_router | None, mode). Direct mode persists to a
    LOCAL ledger under the config home (0.8.1, owner decision): counters survive
    restarts and are coherent across every gateway process on this machine; they are
    still per-machine — fleet truth needs router mode. Both instances share one state
    file (WAL, multi-process safe) exactly like fleet mode shares the VPS one."""
    mode = resolve_mode(app_cfg, router_cfg)
    chain = list(app_cfg.routing.optimal_chain or [])
    want_optimal = bool(chain) and chain != list(router_cfg.chain)
    if mode == "direct":
        state = direct_state_path(router_cfg)
        cfg = router_cfg.model_copy(update={"state_path": state})
        Path(state).parent.mkdir(parents=True, exist_ok=True)
        router = Router(cfg)
        optimal = (Router(cfg.model_copy(update={"chain": chain})) if want_optimal
                   else None)
    else:
        router = Router(router_cfg)
        optimal = (Router(router_cfg.model_copy(update={"chain": chain}))
                   if want_optimal else None)
    return router, optimal, mode
