"""File ingest (M4.5, docs/03-architecture.md §9): HMAC one-time upload tickets,
atomic inbox storage, text sniffing (binary fail-fast, ZERO quota spend), a minimal
server-side RTF→text extractor (D11: the client stays maximally light) and retention
pruning. Upload channels: HTTP PUT to a signed /v1/upload/<id> (signature IS the
auth, bearer-exempt) or plain scp to inbox/<id> (SSH key is the auth)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from pathlib import Path


class IngestError(Exception):
    """Carries a structured JSON-able payload (fail-fast, zero spend)."""

    def __init__(self, error: str, **detail):
        super().__init__(error)
        self.payload = {"error": error, **detail}


# ---------- tickets ----------

def make_ticket(token: str, upload_id: str, exp: int, size: int) -> str:
    return hmac.new(token.encode(), f"{upload_id}|{exp}|{size}".encode(),
                    hashlib.sha256).hexdigest()


def verify_ticket(token: str, upload_id: str, exp: str | int, size: str | int,
                  sig: str) -> None:
    if not token:
        raise IngestError("ingest_disabled", hint="server has no GEMINI_MCP_TOKEN")
    try:
        exp_i, size_i = int(exp), int(size)
    except (TypeError, ValueError):
        raise IngestError("bad_ticket", hint="exp/size must be integers") from None
    if exp_i < time.time():
        raise IngestError("ticket_expired", expired_at=exp_i,
                          hint="issue a fresh file_upload_ticket")
    if not hmac.compare_digest(make_ticket(token, upload_id, exp_i, size_i), sig or ""):
        raise IngestError("bad_signature")


def issue_ticket(token: str, ttl_s: int) -> tuple[str, int]:
    return uuid.uuid4().hex, int(time.time()) + ttl_s


# ---------- storage ----------

def sidecar_path(inbox: Path, upload_id: str) -> Path:
    return inbox / f"{upload_id}.json"


def reserve(inbox: Path, upload_id: str, filename: str, size: int) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    sidecar_path(inbox, upload_id).write_text(json.dumps(
        {"filename": filename, "size_declared": size, "created": time.time()}),
        encoding="utf-8")


def save_upload(inbox: Path, upload_id: str, data: bytes, max_bytes: int) -> Path:
    if len(data) > max_bytes:
        raise IngestError("file_too_large", bytes=len(data), max_bytes=max_bytes)
    if (inbox / upload_id).exists():
        raise IngestError("already_uploaded", upload_id=upload_id,
                          hint="one-time ticket: file exists, call analyze_file")
    inbox.mkdir(parents=True, exist_ok=True)
    part = inbox / f"{upload_id}.part"
    part.write_bytes(data)
    final = inbox / upload_id
    part.rename(final)                      # atomic
    os.chmod(final, 0o600)
    return final


def load_sidecar(inbox: Path, upload_id: str) -> dict:
    p = sidecar_path(inbox, upload_id)
    if not p.exists():
        raise IngestError("upload_not_found", upload_id=upload_id,
                          hint="run file_upload_ticket first, then upload the bytes")
    return json.loads(p.read_text(encoding="utf-8"))


def prune(inbox: Path, keep_hours: float, total_cap_mb: float) -> int:
    """Drop blobs+sidecars older than keep_hours; then oldest-first until the total
    fits the cap. Returns count removed. Piggybacked on ticket issuance."""
    if not inbox.exists():
        return 0
    now = time.time()
    removed = 0
    blobs = [p for p in inbox.iterdir() if p.is_file() and not p.suffix
             and not p.name.endswith(".part")]
    for p in list(blobs):
        if now - p.stat().st_mtime > keep_hours * 3600:
            sidecar_path(inbox, p.name).unlink(missing_ok=True)
            p.unlink(missing_ok=True)
            blobs.remove(p)
            removed += 1
    total = sum(p.stat().st_size for p in blobs)
    for p in sorted(blobs, key=lambda q: q.stat().st_mtime):   # oldest first
        if total <= total_cap_mb * 1_048_576:
            break
        total -= p.stat().st_size
        sidecar_path(inbox, p.name).unlink(missing_ok=True)
        p.unlink(missing_ok=True)
        removed += 1
    return removed


# ---------- text sniffing & decoding ----------

_MAGIC = ((b"%PDF", "pdf"), (b"PK\x03\x04", "zip-container (docx/xlsx/…)"),
          (b"\x1f\x8b", "gzip"), (b"Rar!", "rar"), (b"\x89PNG", "png"),
          (b"\xff\xd8\xff", "jpeg"))


def sniff_binary(data: bytes) -> str | None:
    """Reason string if the blob is not text-like, else None."""
    for magic, name in _MAGIC:
        if data.startswith(magic):
            return name
    if b"\x00" in data[:8192]:
        return "nul-bytes"
    sample = data[:65536]
    weird = sum(1 for b in sample if b < 9 or 13 < b < 32)
    if sample and weird / len(sample) > 0.05:
        return f"non-text byte ratio {weird / len(sample):.0%}"
    return None


def decode_text(data: bytes) -> tuple[str, str]:
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace"), "latin-1(replaced)"


# ---------- minimal RTF → text (D11) ----------

_JUNK_GROUPS = {"fonttbl", "colortbl", "stylesheet", "info", "generator", "pict",
                "themedata", "colorschememapping", "latentstyles", "datastore",
                "mmathPr", "wgrffmtfilter", "missingfont", "upr", "ud"}
_UC_RE = re.compile(r"\\u(-?\d+) ?")     # space = control-word delimiter; the ANSI
                                         # fallback chars are skipped via uc_skip


def rtf_to_text(raw: str) -> str:
    """Byte-preserving latin-1 string in → plain text out. Handles: \'XX runs
    (cp1251 Cyrillic), \\uNNNN (with uc-fallback skip), \\par/\\line → newline,
    \\tab, literal escapes, and skips junk groups ({\\fonttbl…}, {\\pict…}, …)."""
    out: list[str] = []
    depth = 0
    skip_below = -1                 # while depth > skip_below: discard output
    uc_skip = 1                     # chars to drop after \uNNNN
    i, n = 0, len(raw)
    pending_hex = bytearray()

    def flush_hex():
        if pending_hex:
            out.append(pending_hex.decode("cp1251", "replace"))
            pending_hex.clear()

    while i < n:
        c = raw[i]
        if c == "{":
            flush_hex()
            depth += 1
            m = re.match(r"\{\\([a-zA-Z]+)", raw[i:i + 24])
            if m and m.group(1) in _JUNK_GROUPS and skip_below < 0:
                skip_below = depth
            i += 1
            continue
        if c == "}":
            flush_hex()
            if skip_below >= 0 and depth <= skip_below:
                skip_below = -1
            depth -= 1
            i += 1
            continue
        if c == "\\":
            nxt = raw[i + 1] if i + 1 < n else ""
            if nxt == "'":
                try:
                    pending_hex.append(int(raw[i + 2:i + 4], 16))
                except ValueError:
                    flush_hex()
                i += 4
                continue
            flush_hex()
            m = _UC_RE.match(raw, i)
            if m:
                v = int(m.group(1))
                if skip_below < 0:
                    out.append(chr(v + 65536 if v < 0 else v))
                i = m.end() + uc_skip       # drop the ANSI fallback char
                continue
            m = re.match(r"\\([a-zA-Z]+)(-?\d+)? ?", raw[i:])
            if m:
                word = m.group(1)
                if word in ("par", "line", "sect") and skip_below < 0:
                    out.append("\n")
                elif word == "tab" and skip_below < 0:
                    out.append("\t")
                elif word == "uc":
                    uc_skip = max(0, int(m.group(2) or 1))
                i += m.end()
                continue
            if nxt in "{\\}":
                if skip_below < 0:
                    out.append(nxt)
                i += 2
                continue
            i += 1                          # \<other char>: drop the backslash
            continue
        flush_hex()
        if skip_below < 0 and c not in "\r\n":
            out.append(c)
        elif skip_below < 0:
            out.append("\n" if c == "\n" else "")
        i += 1
    flush_hex()
    text = "".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_text(data: bytes, filename: str) -> tuple[str, str, str]:
    """(text, encoding, extractor). RTF → rtf_to_text; else decode_text."""
    if data[:64].lstrip().startswith(b"{\\rtf") or filename.lower().endswith(".rtf"):
        return rtf_to_text(data.decode("latin-1")), "rtf(cp1251-escapes)", "rtf"
    text, enc = decode_text(data)
    return text, enc, "plain"
