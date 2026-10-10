"""Dual-mode backends. 'router' = fleet mode: shared SQLite ledger (VPS daemon + SSH
stdio — quota truth shared by every client). 'direct' = plain Google AI Studio: the SAME
Router engine, REST protocol and error vocabulary, with a LOCAL persistent ledger under
the config home (~/.config/gemini-mcp/state.db) — no server, counters survive restarts
and are shared by all gateway processes on this machine.

Mode: `backend: auto|direct|router` in config.yaml or GEMINI_MCP_BACKEND. auto → 'router'
only when persistent state exists (GEMINI_ROUTER_CONFIG set or state_path file present),
else 'direct' — the consumer default with zero setup beyond API keys.

Throwaway work (a live key probe) needs no separate ledger class: `state_path:
":memory:"` gives the real Ledger a zero-disk SQLite database."""
from __future__ import annotations

import os
from pathlib import Path

from gemini_router import Router
from gemini_router.config import Config as RouterConfig

from .config import AppCfg


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
