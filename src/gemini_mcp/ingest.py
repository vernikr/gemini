"""File ingest (M4.5, docs/03-architecture.md §10): HMAC one-time upload tickets,
atomic inbox storage, text sniffing (binary fail-fast, ZERO quota spend), a minimal
server-side RTF→text extractor (D11: the client stays maximally light) and retention
pruning. Upload channels: HTTP PUT to a signed /v1/upload/<id> (signature IS the
auth, bearer-exempt) or plain scp to inbox/<id> (SSH key is the auth).

Input routing: text the server can extract (pandoc set, RTF, plain) keeps the text
pipeline; PDF, images, audio and video go to the provider's Files API — `media_kind`
decides, `sniff_binary` still rejects everything that is neither."""
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


def claim_path(inbox: Path, upload_id: str) -> Path:
    return inbox / f"{upload_id}.consumed"


def ticket_spent(inbox: Path, upload_id: str) -> bool:
    return claim_path(inbox, upload_id).exists()


def ticket_consumed_error(upload_id: str) -> IngestError:
    """One wording for both refusal points: the route's cheap pre-check and the
    authoritative claim."""
    return IngestError("ticket_consumed", upload_id=upload_id,
                       hint="one-time ticket already spent — if the bytes landed, call "
                            "analyze_file; otherwise issue a fresh file_upload_ticket")


def claim_ticket(inbox: Path, upload_id: str) -> None:
    """Spend the ticket, exactly once. O_EXCL is the guarantee: atomic between
    concurrent PUTs, and the marker outlives the blob — so a replay inside the TTL is
    refused even once the file is gone (prune, cap eviction, manual cleanup)."""
    inbox.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(claim_path(inbox, upload_id),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ticket_consumed_error(upload_id) from None
    os.write(fd, f"{time.time():.0f}\n".encode())
    os.close(fd)


def save_upload(inbox: Path, upload_id: str, data: bytes, max_bytes: int) -> Path:
    if len(data) > max_bytes:
        raise IngestError("file_too_large", bytes=len(data), max_bytes=max_bytes)
    if (inbox / upload_id).exists():
        raise IngestError("already_uploaded", upload_id=upload_id,
                          hint="one-time ticket: file exists, call analyze_file")
    # Spent BEFORE the bytes land: a crash between the claim and the write costs a
    # re-issued ticket, never a replay window.
    claim_ticket(inbox, upload_id)
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


def _drop(inbox: Path, name: str) -> None:
    """A blob and everything belonging to its ticket (sidecar, spent marker): past
    keep_hours the claim is many TTLs old, so forgetting it is safe."""
    for p in (inbox / name, sidecar_path(inbox, name), claim_path(inbox, name)):
        p.unlink(missing_ok=True)


def _evict(inbox: Path, name: str) -> None:
    """Cap eviction: the bytes go, the spent marker STAYS — evicting the oldest blob
    must not reopen a replay window for a ticket still inside its TTL."""
    for p in (inbox / name, sidecar_path(inbox, name)):
        p.unlink(missing_ok=True)


def prune(inbox: Path, keep_hours: float, total_cap_mb: float) -> int:
    """Drop blobs older than keep_hours; then oldest-first until the total fits the
    cap. Returns count removed. Triggers: ticket issuance, a completed analyze_file
    and `gemini prune` — an inbox nobody asks tickets from still ages out."""
    if not inbox.exists():
        return 0
    now = time.time()
    older_than = now - keep_hours * 3600
    removed = 0
    blobs = [p for p in inbox.iterdir() if p.is_file() and not p.suffix]
    for p in list(blobs):
        if p.stat().st_mtime < older_than:
            _drop(inbox, p.name)
            blobs.remove(p)
            removed += 1
    # Markers, sidecars and half-written parts outlive their blob only when it was
    # removed outside prune; they age out on the same clock. The ticket TTL is orders
    # of magnitude below keep_hours, so nothing replayable is discarded here.
    for p in inbox.iterdir():
        if p.suffix in (".json", ".consumed", ".part") \
                and p.stat().st_mtime < older_than:
            p.unlink(missing_ok=True)
    total = sum(p.stat().st_size for p in blobs)
    for p in sorted(blobs, key=lambda q: q.stat().st_mtime):   # oldest first
        if total <= total_cap_mb * 1_048_576:
            break
        total -= p.stat().st_size
        _evict(inbox, p.name)
        removed += 1
    return removed


# ---------- provider-bound media (Files API) ----------

# What the provider reads natively. Magic first, extension only as a hint for files
# whose container we cannot fingerprint — and only when the bytes are not text, so a
# plain-text file named .mp4 keeps going down the text path.
_MEDIA_MAGIC = (
    (b"%PDF", "pdf", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image", "image/png"),
    (b"\xff\xd8\xff", "image", "image/jpeg"),
    (b"GIF87a", "image", "image/gif"),
    (b"GIF89a", "image", "image/gif"),
    (b"OggS", "audio", "audio/ogg"),
    (b"fLaC", "audio", "audio/flac"),
    (b"ID3", "audio", "audio/mpeg"),
)
_MEDIA_EXTS = {
    "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg",
    "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif",
    "heic": "image/heic", "heif": "image/heif", "tif": "image/tiff",
    "tiff": "image/tiff", "bmp": "image/bmp",
    "wav": "audio/wav", "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac",
    "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/opus", "flac": "audio/flac",
    "mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime",
    "webm": "video/webm", "avi": "video/x-msvideo", "mkv": "video/x-matroska",
}


def media_kind(data: bytes, filename: str = "") -> tuple[str, str] | None:
    """(kind, mime) when the provider should read the file itself, else None."""
    for magic, kind, mime in _MEDIA_MAGIC:
        if data.startswith(magic):
            return kind, mime
    if data[4:8] == b"ftyp":                       # ISO-BMFF: mp4/mov/m4a share the box
        brand = data[8:12].decode("latin-1", "replace")
        return ("audio", "audio/mp4") if brand.startswith(("M4A", "mp4a", "M4B")) \
            else ("video", "video/mp4")
    if data.startswith(b"RIFF"):
        if data[8:12] == b"WAVE":
            return "audio", "audio/wav"
        if data[8:12] == b"WEBP":
            return "image", "image/webp"
    if data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio", "audio/mpeg"               # MP3 frame sync, no ID3 tag
    low = filename.lower()
    mime = _MEDIA_EXTS.get(low.rsplit(".", 1)[-1]) if "." in low else None
    if mime and sniff_binary(data, filename, pandoc_ok=False) is not None:
        return mime.split("/")[0], mime            # binary + a media name: trust the name
    return None


def pdf_pages(path: Path, timeout_s: int = 30) -> int | None:
    """Page count via pdfinfo (poppler). None when the tool is missing or the PDF is
    unreadable — the provider stays the authority; this check only refuses early."""
    import shutil
    import subprocess

    exe = shutil.which("pdfinfo")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, str(path)], capture_output=True, timeout=timeout_s,
                             check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(rb"^Pages:\s+(\d+)", out.stdout, re.M)
    return int(m.group(1)) if m else None


def expires_ts(value: str | None) -> float | None:
    """Provider `expirationTime` (RFC 3339) as a unix timestamp, None when unparsable."""
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def save_provider_ref(inbox: Path, upload_id: str, ref: dict) -> None:
    """Remember which provider file carries this blob, so a second analyze_file on the
    same id re-uploads only once the reference expired (and the blob is immutable for a
    given upload_id, so the reference cannot go stale while it exists)."""
    side = load_sidecar(inbox, upload_id)
    side["provider"] = {**ref, "saved": time.time()}
    sidecar_path(inbox, upload_id).write_text(json.dumps(side), encoding="utf-8")


# ---------- text sniffing & decoding ----------

_MAGIC = ((b"%PDF", "pdf"), (b"PK\x03\x04", "zip-container (docx/xlsx/…)"),
          (b"\x1f\x8b", "gzip"), (b"Rar!", "rar"), (b"\x89PNG", "png"),
          (b"\xff\xd8\xff", "jpeg"))

# documents pandoc turns into Markdown (extension-gated: content sniffing of
# OOXML internals is out of scope)
_PANDOC_EXTS = (".docx", ".odt", ".epub", ".html", ".htm")


def sniff_binary(data: bytes, filename: str = "", pandoc_ok: bool = True) -> str | None:
    """Reason string if the blob is not analyzable, else None. pandoc-convertible
    containers (docx/odt/epub/html) pass when pandoc_ok — the server converts them."""
    low = filename.lower()
    if pandoc_ok and low.endswith(_PANDOC_EXTS):
        return None
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


def pandoc_to_markdown(data: bytes, fmt: str, timeout_s: int = 60) -> str | None:
    """One-shot subprocess: bytes → GitHub-flavored Markdown (headings, bold/italic,
    lists and tables survive). None when pandoc is missing or fails — callers fall
    back to the plain-text extractor. No daemon, no RAM residency (deployment notes)."""
    import shutil
    import subprocess
    import tempfile

    exe = shutil.which("pandoc")
    if not exe:
        return None
    src = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as f:
            f.write(data)
            src = f.name
        out = subprocess.run(
            [exe, "-f", fmt, "-t", "gfm", "--wrap=none", src],
            capture_output=True, timeout=timeout_s, check=False)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.decode("utf-8", "replace")
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if src:
            Path(src).unlink(missing_ok=True)


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


def extract_text(data: bytes, filename: str, use_pandoc: bool = True,
                 pandoc_timeout_s: int = 60) -> tuple[str, str, str]:
    """(text, encoding-or-format, extractor). Pandoc-convertible documents
    (RTF/DOCX/ODT/EPUB/HTML) become GitHub-flavored Markdown when pandoc is
    available — headings, bold/italic, lists and tables survive for the model.
    RTF additionally has the built-in plain-text fallback; everything else is
    decoded as text."""
    low = filename.lower()
    is_rtf = data[:64].lstrip().startswith(b"{\\rtf") or low.endswith(".rtf")
    if use_pandoc:
        fmt = ""
        if is_rtf:
            fmt = "rtf"
        elif low.endswith(_PANDOC_EXTS):
            fmt = low.rsplit(".", 1)[-1]
        if fmt:
            md = pandoc_to_markdown(data, fmt, pandoc_timeout_s)
            if md is not None:
                return md, "markdown", f"pandoc/{fmt}→gfm"
            if not is_rtf:
                raise IngestError(
                    "conversion_unavailable", filename=filename,
                    hint="pandoc is not installed on the server — this container "
                         "cannot be converted yet (zero quota spent)")
    if is_rtf:
        return rtf_to_text(data.decode("latin-1")), "rtf(cp1251-escapes)", "rtf"
    text, enc = decode_text(data)
    return text, enc, "plain"
