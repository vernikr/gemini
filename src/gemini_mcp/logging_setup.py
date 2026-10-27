"""Logging & eviction (FR-E4..E8): rich console → stderr (journald-friendly), rotating
app.log, daily JSONL request dumps, secret redaction everywhere, retention pruning."""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterable
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

from .config import LoggingCfg

SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{8,}"),        # Gemini API keys (Standard, Cloud Console)
    re.compile(r"AQ\.[0-9A-Za-z_\-]{8,}"),         # Gemini API keys (Auth, AI Studio)
    re.compile(r"ya29\.[0-9A-Za-z_\-]+"),          # Google OAuth tokens
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),           # GitHub PATs
]


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub("***", text)
    for sec in extra_secrets:
        if sec and len(sec) >= 8:
            text = text.replace(sec, "***")
    return text


class RedactionFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        clean = redact(msg, self.secrets)
        if clean != msg:
            record.msg = clean
            record.args = ()
        return True


def setup_logging(cfg: LoggingCfg, secrets: Iterable[str] = ()) -> logging.Logger:
    logger = logging.getLogger("gemini_mcp")
    logger.handlers.clear()
    logger.setLevel(cfg.level.upper())
    logger.propagate = False
    filt = RedactionFilter(secrets)

    console_handler = RichHandler(console=Console(stderr=True), show_path=False,
                                  rich_tracebacks=True, markup=False)
    console_handler.addFilter(filt)
    logger.addHandler(console_handler)

    log_dir = Path(cfg.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(log_dir / "app.log", maxBytes=cfg.max_bytes,
                                       backupCount=cfg.backups, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    file_handler.addFilter(filt)
    logger.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logger


class AuditLogger:
    """One JSON line per tool call (FR-E5). Bodies only when dump_bodies is on."""

    def __init__(self, cfg: LoggingCfg, secrets: Iterable[str] = ()):
        self.cfg = cfg
        self.dir = Path(cfg.dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.secrets = [s for s in secrets if s]

    def path_for(self, when: float | None = None) -> Path:
        d = datetime.fromtimestamp(when or time.time()).strftime("%Y-%m-%d")
        return self.dir / f"requests-{d}.jsonl"

    def record(self, entry: dict) -> Path:
        line = redact(json.dumps(entry, ensure_ascii=False, default=str), self.secrets)
        path = self.path_for()
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return path

    def tail(self, n: int = 20, date: str | None = None) -> list[dict]:
        path = self.dir / f"requests-{date}.jsonl" if date else self.path_for()
        if not path.exists():
            candidates = sorted(self.dir.glob("requests-*.jsonl"))
            if not candidates:
                return []
            path = candidates[-1]
        lines = path.read_text(encoding="utf-8").splitlines()[-n:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out


def prune_jsonl(cfg: LoggingCfg, now: float | None = None, dry: bool = False) -> list[str]:
    """Delete requests-*.jsonl older than jsonl_keep_days (FR-E6)."""
    now = now or time.time()
    cutoff = (datetime.fromtimestamp(now) - timedelta(days=cfg.jsonl_keep_days)).date()
    removed = []
    for path in sorted(Path(cfg.dir).glob("requests-*.jsonl")):
        try:
            d = datetime.strptime(path.stem.removeprefix("requests-"), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            removed.append(str(path))
            if not dry:
                path.unlink(missing_ok=True)
    return removed


def enforce_audit_size(ledger, max_mb: float) -> dict:
    """Shrink the audit tables until the state db fits `max_mb` (FR-E6).

    DELETE never shrinks a SQLite file, so the prune has to be followed by a VACUUM —
    without it a retention window can be halved forever and the file will not lose a
    byte. Steps down progressively so a slightly-over cap keeps most of the history.
    """
    out = {"cap_met": True, "keep_days_used": None, "size_mb": 0.0}
    path = Path(ledger.path)
    if not path.exists():
        return out
    out["size_mb"] = path.stat().st_size / 1e6
    if out["size_mb"] <= max_mb:
        return out
    for days in (30.0, 7.0, 1.0):
        ledger.prune(audit_keep_days=days)
        ledger.vacuum()
        stats = ledger.stats()
        empty = not any(stats.get(k) for k in ("calls", "cache", "jobs"))
        # An empty v4 ledger still owns its tables and indexes (~0.1 MB). That is the
        # floor: no retention window can shrink it further, so the cap is met.
        out.update(keep_days_used=days, size_mb=path.stat().st_size / 1e6,
                   cap_met=empty or path.stat().st_size <= max_mb * 1e6)
        if out["cap_met"]:
            return out
    return out
