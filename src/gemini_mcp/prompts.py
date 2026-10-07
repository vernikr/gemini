"""Prompt templates (FR-D3, FR-D6). Editable constants; a prompts/ dir override can be
added later without touching logic (D8)."""
from __future__ import annotations

CHUNK_MARKER = (
    "[SYSTEM NOTE — CHUNK {n}/{m}] You are analyzing part {n} of {m} of ONE larger "
    "document; neighbouring parts intentionally overlap. Analyze ONLY this part "
    "according to the task. Do NOT summarize the whole document and do NOT speculate "
    "about parts you cannot see. If a thought continues beyond this part, end your "
    "answer with '…(continues)'."
)

CONSOLIDATION_NOTE = (
    "[SYSTEM NOTE — CONSOLIDATION round {round}] The analyses below were produced "
    "from parts of ONE document (parts overlapped)."
)

FINAL_RULES = (
    " Merge them into ONE coherent final result for the task: deduplicate "
    "overlap artifacts, join fragments ending with '…(continues)', keep every distinct "
    "fact, preserve the document's order. Output ONLY the final result — no "
    "meta-commentary, no mentions of parts or chunks."
)

GROUP_RULES = (
    " Merge THESE analyses into one intermediate summary (they will be consolidated "
    "again later). Keep every distinct fact, deduplicate overlaps, no meta-commentary."
)


def chunk_prompt(task: str, n: int, m: int, chunk_text: str) -> str:
    return (f"{CHUNK_MARKER.format(n=n, m=m)}\n\nTASK:\n{task}\n\n"
            f"PART {n}/{m}:\n{chunk_text}")


def consolidation_prompt(task: str, parts: list[tuple[int, str]], total_m: int,
                         missing: list[int], round_no: int = 1, final: bool = True) -> str:
    head = CONSOLIDATION_NOTE.format(round=round_no) + (FINAL_RULES if final else GROUP_RULES)
    if missing:
        head += (f" Parts {missing} of {total_m} failed to analyze — do NOT invent their "
                 f"content; mention the gap only if it matters for the task.")
    body = "\n\n".join(f"--- ANALYSIS OF PART {i}/{total_m} ---\n{text}" for i, text in parts)
    return f"{head}\n\nTASK (original):\n{task}\n\n{body}"
