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
    # Empirical availability order: older models are less loaded on the free tier.
    # Deprecated ids stay out — 3.5 and 3.7 are aliases now, so a call to them lands on
    # 3.6 / 3.8 anyway, and counting it against the alias's own budget would be wrong.
    optimal_chain: list[str] = Field(default_factory=lambda: [
        "gemini-3.6-flash", "gemini-3-flash-preview", "gemini-3.8-flash"])


class IngestCfg(BaseModel):
    """File ingest (M4.5): inbox dir, caps, ticket TTL, retention (§9).
    M4.6: pandoc converts RTF/DOCX → GitHub-flavored Markdown server-side."""
    dir: str = "inbox"
    # 20 MB keeps a single job inside the service's memory allowance. The gateway
    # holds the extracted document in memory while it chunks, so a larger file can
    # push the unit past MemoryMax and kill every in-flight job with it. An agent may
    # exceed it per call with override_size_limit, on the user's explicit say-so.
    max_file_mb: int = 20
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
    backend: str = "auto"                    # auto|direct|router (backends.py)


def parse_keys(text: str) -> list[str]:
    """Keys from a file/argument: comma- and/or whitespace-separated, `#` comments
    allowed, order preserved, deduped. Gemini keys are [A-Za-z0-9_-] so no quoting."""
    out: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        out += [t for t in line.replace(",", " ").split() if t]
    return list(dict.fromkeys(out))


def config_home() -> Path:
    """Wizard-managed config dir: $GEMINI_MCP_HOME or ~/.config/gemini-mcp (XDG-ish,
    same on macOS/Linux). setup writes config.yaml + .env (0600) here."""
    return Path(os.environ.get("GEMINI_MCP_HOME")
                or Path.home() / ".config" / "gemini-mcp")


def load_all(path: str | Path | None = None, **over) -> tuple[AppCfg, RouterConfig]:
    p = path or os.environ.get("GEMINI_MCP_CONFIG")
    if not p and Path("config.yaml").exists():
        p = "config.yaml"
    if not p:                                   # consumer default (setup wizard output)
        cand = config_home() / "config.yaml"
        p = cand if cand.exists() else None
    raw: dict = {}
    if p and Path(p).exists():
        raw = yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {}
    app = AppCfg.model_validate({k: raw[k] for k in
                                 ("pipeline", "logging", "server", "thinking",
                                  "routing", "ingest", "backend") if k in raw})
    if not raw:
        # bare launch (client JSON pasted without `setup`): cwd is arbitrary — MCP
        # clients spawn with cwd=$HOME — so keep rotating logs out of ./logs (~/logs)
        # and beside the state.db that direct mode already puts in the config home
        app.logging.dir = str(config_home() / "logs")
    router_raw = dict(raw.get("router", {}))
    # Key sources (highest wins): explicit over['api_keys'] (CLI --keys/--keys-file)
    # → keys file (router.keys_file in config.yaml / GEMINI_MCP_KEYS_FILE env)
    # → router.api_keys in config.yaml → GEMINI_API_KEYS[_1..10] env (.env bootstrap)
    kf = router_raw.pop("keys_file", None) or os.environ.get("GEMINI_MCP_KEYS_FILE")
    if kf:
        kfp = Path(str(kf)).expanduser()
        if not kfp.is_file():
            raise FileNotFoundError(f"keys file not found: {kfp}")
        fkeys = parse_keys(kfp.read_text(encoding="utf-8"))
        if fkeys:
            router_raw["api_keys"] = fkeys
    router_cfg = RouterConfig.load(os.environ.get("GEMINI_ROUTER_CONFIG"),
                                   **{**router_raw,
                                      **{k: v for k, v in over.items() if v is not None}})
    return app, router_cfg
