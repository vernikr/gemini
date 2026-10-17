"""Prompt templates (FR-D3, FR-D6). Editable constants; a prompts/ dir override can be
added later without touching logic (D8)."""
from __future__ import annotations

BREVITY_RULE = (
    "ask Gemini for a SHORT answer by default — say so in words inside the task "
    '("answer in 3-5 sentences", "conclusions only"). Ask for the full treatment only '
    "when the task actually calls for it. Nothing on the server shortens an answer for "
    "you: there is no length cap, so an over-long request is returned in full and lands "
    "in your context. meta.result_chars says how long what you got is."
)
"""One canonical statement of the length policy, shared by every surface that advises
the caller: the MCP instructions, the CLI help, and the skill. Kept here rather than
duplicated so the three cannot drift apart — a parity test asserts each surface
contains this exact text."""

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


GROUP_MARKER = (
    "[SYSTEM NOTE — GROUP {n}/{m}] You are analyzing group {n} of {m}: a set of "
    "separate files that belong to ONE common task. Analyze the files of THIS group "
    "together, referring to each file by name. Do NOT speculate about files outside "
    "this group."
)


def group_prompt(task: str, n: int, m: int, filenames: str) -> str:
    return (f"{GROUP_MARKER.format(n=n, m=m)}\nFiles in this group: {filenames}.\n\n"
            f"TASK:\n{task}")


SPLIT_MARKER = (
    "[SYSTEM NOTE — DOCUMENT PART {n}/{m}] The PDF document '{name}' alone exceeded "
    "the provider's per-request token budget and was split by page ranges. This "
    "request carries part {n} of {m} ({pages}); every file in this request is a part "
    "of THIS one document — read them in order and analyze them according to the task "
    "as if reading the whole document. Do NOT speculate about parts you cannot see; if "
    "a thought continues beyond this part, end your answer with '…(continues)'."
)


def split_prompt(task: str, name: str, n: int, m: int, pages: str) -> str:
    return (f"{SPLIT_MARKER.format(n=n, m=m, name=name, pages=pages)}\n\nTASK:\n{task}")


def consolidation_prompt(task: str, parts: list[tuple[int, str]], total_m: int,
                         missing: list[int], round_no: int = 1, final: bool = True) -> str:
    head = CONSOLIDATION_NOTE.format(round=round_no) + (FINAL_RULES if final else GROUP_RULES)
    if missing:
        head += (f" Parts {missing} of {total_m} failed to analyze — do NOT invent their "
                 f"content; mention the gap only if it matters for the task.")
    body = "\n\n".join(f"--- ANALYSIS OF PART {i}/{total_m} ---\n{text}" for i, text in parts)
    return f"{head}\n\nTASK (original):\n{task}\n\n{body}"
