"""Token-aware splitter (FR-D2): paragraph → sentence → line → hard cut; overlap by
walking segment windows back. Character budgets come from the pipeline using the real
chars/token ratio measured by countTokens."""
from __future__ import annotations

import math
import re
from collections.abc import Iterator

PARA_RE = re.compile(r".+?(?:\n\s*\n|$)", re.S)
SENT_RE = re.compile(r".+?(?:[.!?…]+(?:\s|$)|\n|$)", re.S)


def segment(text: str, max_chars: int) -> Iterator[str]:
    """Yield units ≤ max_chars, preserving separators (lossless join).

    A generator, not a list. Materialising every segment of a multi-megabyte
    document costs more than the document itself — hundreds of thousands of small
    strings, each carrying its own object overhead — and the only caller consumes
    them in order, once.
    """
    for para in (m.group(0) for m in PARA_RE.finditer(text)):
        if len(para) <= max_chars:
            yield para
            continue
        for sent in (m.group(0) for m in SENT_RE.finditer(para)):
            if len(sent) <= max_chars:
                yield sent
                continue
            for line in sent.splitlines(keepends=True):
                if len(line) <= max_chars:
                    yield line
                    continue
                for i in range(0, len(line), max_chars):
                    yield line[i:i + max_chars]


def split_text(body: str, max_chars: int, overlap_chars: int = 0,
               merge_tail_ratio: float = 0.05) -> list[str]:
    """Cut into ≤ max_chars chunks, carrying a tail of at most overlap_chars into the
    next one. Holds only the chunk under construction, never the whole segmentation."""
    if not body:
        return []
    max_chars = max(1, int(max_chars))
    overlap_chars = max(0, min(int(overlap_chars), max_chars // 2))
    chunks: list[str] = []
    win: list[str] = []          # segments of the chunk being built
    size = 0
    for seg in segment(body, max_chars):
        if win and size + len(seg) > max_chars:
            chunks.append("".join(win))
            # Carry the tail back for the overlap. A single-segment chunk carries
            # nothing, and at least one segment is always dropped, so every pass
            # consumes strictly more of the body than the last.
            keep: list[str] = []
            if overlap_chars > 0 and len(win) > 1:
                acc = 0
                for s in reversed(win):
                    if acc >= overlap_chars:
                        break
                    keep.append(s)
                    acc += len(s)
                keep.reverse()
                if len(keep) >= len(win):
                    keep = keep[1:]
            win = keep
            size = sum(map(len, win))
        win.append(seg)
        size += len(seg)
    if win:
        chunks.append("".join(win))
    if len(chunks) >= 2 and len(chunks[-1]) < merge_tail_ratio * max_chars:
        tail = chunks.pop()
        chunks[-1] += tail          # NB: not chunks[-2] += pop() — negative index shifts
    return chunks


def plan_chunks(n_tokens: int, chunk_tokens: int, overlap_ratio: float) -> int:
    """Estimate M for progress/plan messages; the real M comes from split_text."""
    if n_tokens <= chunk_tokens:
        return 1
    stride = max(1, int(chunk_tokens * (1 - min(max(overlap_ratio, 0.0), 0.5))))
    return max(1, math.ceil((n_tokens - chunk_tokens * overlap_ratio) / stride))
