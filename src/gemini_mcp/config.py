"""App config: one config.yaml (sections router/pipeline/logging/server/thinking).
The `router` section is handed to gemini_router.Config; env secrets stay in .env."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from gemini_router import Config as RouterConfig
from pydantic import BaseModel, Field


class PipelineCfg(BaseModel):
    chunk_threshold_tokens: int = 200_000   # > this (measured) → chunking path
    chunk_size_tokens: int = 180_000        # TPM headroom under 250k
    overlap_ratio: float = 0.10
    merge_tail_ratio: float = 0.05          # merge last chunk if < 5% of budget
    consolidation_fanin: int = 6            # recursive tree-reduce group size
    fail_on_chunk_error: bool = True        # false → continue with marked gaps (FR-D7)
    max_result_chars: int = 12_000


class LoggingCfg(BaseModel):
    dir: str = "logs"
    max_bytes: int = 10_485_760             # app.log rotation size (FR-E4)
    backups: int = 7
    jsonl_keep_days: int = 14               # requests-*.jsonl eviction (FR-E6)
    audit_keep_days: int = 30
    audit_max_mb: int = 50
    dump_bodies: bool = False               # FR-E8: bodies only on explicit opt-in
    level: str = "INFO"


class ServerCfg(BaseModel):
    http_bind: str = "127.0.0.1"            # b06: loopback only; tunnels for remote
    http_port: int = 8790
    mcp_path: str = "/mcp"


class ThinkingCfg(BaseModel):
    default: str = "medium"                 # overridable per request (FR-D, user decision)


class RoutingCfg(BaseModel):
    """Two fallback routes: 'optimal' (default) walks models in empirical
    availability order and auto-overflows to flash-lite/gemma; 'best_available'
    keeps the newest-first config chain with opt-in overflow (legacy semantics)."""
    default_route: str = "optimal"
    optimal_chain: list[str] = Field(default_factory=lambda: [
        "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash-preview",
        "gemini-3.8-flash", "gemini-3.7-flash"])


class IngestCfg(BaseModel):
    """File ingest (M4.5): inbox dir, caps, ticket TTL, retention (§9).
    M4.6: pandoc converts RTF/DOCX → GitHub-flavored Markdown server-side."""
    dir: str = "inbox"
    max_file_mb: int = 50
    ticket_ttl_s: int = 900
    keep_hours: float = 24
    total_cap_mb: float = 500
    scp_target: str = ""           # e.g. root@<vps>; shown in tickets when set
    use_pandoc: bool = True        # RTF/DOCX/ODT/EPUB/HTML → gfm Markdown
    pandoc_timeout_s: int = 60


class AppCfg(BaseModel):
    pipeline: PipelineCfg = Field(default_factory=PipelineCfg)
    logging: LoggingCfg = Field(default_factory=LoggingCfg)
    server: ServerCfg = Field(default_factory=ServerCfg)
    thinking: ThinkingCfg = Field(default_factory=ThinkingCfg)
    routing: RoutingCfg = Field(default_factory=RoutingCfg)
    ingest: IngestCfg = Field(default_factory=IngestCfg)


def load_all(path: str | Path | None = None, **over) -> tuple[AppCfg, RouterConfig]:
    p = path or os.environ.get("GEMINI_MCP_CONFIG")
    if not p and Path("config.yaml").exists():
        p = "config.yaml"
    raw: dict = {}
    if p and Path(p).exists():
        raw = yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {}
    app = AppCfg.model_validate({k: raw[k] for k in
                                 ("pipeline", "logging", "server", "thinking",
                                  "routing", "ingest") if k in raw})
    router_cfg = RouterConfig.load(os.environ.get("GEMINI_ROUTER_CONFIG"),
                                   **raw.get("router", {}))
    return app, router_cfg
