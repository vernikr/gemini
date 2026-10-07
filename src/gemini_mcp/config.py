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
    default: str = "high"                   # overridable per request (FR-D, user decision)


class AppCfg(BaseModel):
    pipeline: PipelineCfg = Field(default_factory=PipelineCfg)
    logging: LoggingCfg = Field(default_factory=LoggingCfg)
    server: ServerCfg = Field(default_factory=ServerCfg)
    thinking: ThinkingCfg = Field(default_factory=ThinkingCfg)


def load_all(path: str | Path | None = None, **over) -> tuple[AppCfg, RouterConfig]:
    p = path or os.environ.get("GEMINI_MCP_CONFIG")
    if not p and Path("config.yaml").exists():
        p = "config.yaml"
    raw: dict = {}
    if p and Path(p).exists():
        raw = yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {}
    app = AppCfg.model_validate({k: raw[k] for k in
                                 ("pipeline", "logging", "server", "thinking") if k in raw})
    router_cfg = RouterConfig.load(os.environ.get("GEMINI_ROUTER_CONFIG"),
                                   **raw.get("router", {}))
    return app, router_cfg
