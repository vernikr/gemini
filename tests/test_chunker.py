"""Splitter contracts (FR-D2): lossless, boundary-respecting, overlap, hard cuts, tail."""
from __future__ import annotations

from gemini_mcp.chunker import plan_chunks, segment, split_text


def para(i: int) -> str:
    return f"Paragraph {i}: " + "word " * 14 + "\n\n"


def test_lossless_without_overlap():
    body = "".join(para(i) for i in range(10))
    chunks = split_text(body, 200, 0)
    assert "".join(chunks) == body
    assert all(len(c) <= 200 for c in chunks)
    assert len(chunks) > 1


def test_overlap_shares_tail_with_next_chunk():
    body = "".join(para(i) for i in range(12))
    chunks = split_text(body, 200, 60)
    assert len(chunks) > 2
    shared = sum(1 for a, b in zip(chunks, chunks[1:], strict=False) if a[-15:] in b)
    assert shared >= len(chunks) - 2


def test_hard_split_single_huge_paragraph():
    body = "x" * 1000
    chunks = split_text(body, 100, 0)
    assert "".join(chunks) == body
    assert len(chunks) == 10 and all(len(c) <= 100 for c in chunks)


def test_respects_sentence_boundaries():
    body = " ".join(f"Sentence number {i}." for i in range(100))
    chunks = split_text(body, 120, 0)
    for c in chunks:
        assert c.rstrip().endswith(".")


def test_tail_merge():
    body = "y" * 300 + "\n\n" + "z"
    chunks = split_text(body, 100, 0, merge_tail_ratio=0.05)
    assert len(chunks) == 3 and chunks[-1].endswith("z")


def test_empty_and_segment_budget():
    assert split_text("", 100) == []
    body = "".join(para(i) for i in range(5))
    assert all(len(s) <= 50 for s in segment(body, 50))


def test_plan_chunks_estimate():
    assert plan_chunks(50, 100, 0.1) == 1
    assert plan_chunks(1000, 100, 0.1) == 11
