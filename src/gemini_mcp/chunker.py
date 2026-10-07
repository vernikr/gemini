"""Token-aware splitter (FR-D2): paragraph → sentence → line → hard cut; overlap by
walking segment windows back. Character budgets come from the pipeline using the real
chars/token ratio measured by countTokens."""
from __future__ import annotations

import math
import re

PARA_RE = re.compile(r".+?(?:\n\s*\n|$)", re.S)
SENT_RE = re.compile(r".+?(?:[.!?…]+(?:\s|$)|\n|$)", re.S)


def segment(text: str, max_chars: int) -> list[str]:
    """Split into units ≤ max_chars, preserving separators (lossless join)."""
    segs: list[str] = []
    for para in PARA_RE.findall(text):
        if len(para) <= max_chars:
            segs.append(para)
            continue
        for sent in SENT_RE.findall(para):
            if len(sent) <= max_chars:
                segs.append(sent)
                continue
            for line in sent.splitlines(keepends=True):
                if len(line) <= max_chars:
                    segs.append(line)
                    continue
                for i in range(0, len(line), max_chars):
                    segs.append(line[i:i + max_chars])
    return segs


def split_text(body: str, max_chars: int, overlap_chars: int = 0,
               merge_tail_ratio: float = 0.05) -> list[str]:
    if not body:
        return []
    max_chars = max(1, int(max_chars))
    overlap_chars = max(0, min(int(overlap_chars), max_chars // 2))
    segs = segment(body, max_chars)
    chunks: list[str] = []
    start = 0
    while start < len(segs):
        end, size = start, 0
        while end < len(segs) and size + len(segs[end]) <= max_chars:
            size += len(segs[end])
            end += 1
        chunks.append("".join(segs[start:end]))
        if end >= len(segs):
            break
        if overlap_chars <= 0:
            start = end
            continue
        o, acc = end, 0
        while o > start and acc < overlap_chars:
            o -= 1
            acc += len(segs[o])
        start = max(o, start + 1) if start + 1 < end else end   # guarantee progress
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
