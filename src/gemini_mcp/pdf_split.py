"""Page-range PDF slicing by qpdf: subprocess, no full-document copy in RAM."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class PdfSplitError(Exception):
    pass


def split_pages(src: Path, dest: Path, first: int, last: int) -> None:
    if first < 1 or last < first:
        raise PdfSplitError("invalid PDF page range")
    exe = shutil.which("qpdf")
    if not exe:
        raise PdfSplitError("qpdf is not installed on the server")
    try:
        result = subprocess.run(
            [exe, str(src), "--pages", ".", f"{first}-{last}", "--", str(dest)],
            capture_output=True, timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PdfSplitError(f"qpdf could not cut pages {first}-{last}: {e}") from e
    if result.returncode or not dest.is_file() or not dest.stat().st_size:
        raise PdfSplitError(f"qpdf failed on pages {first}-{last}: "
                            f"{result.stderr.decode(errors='replace')[:300]}")
