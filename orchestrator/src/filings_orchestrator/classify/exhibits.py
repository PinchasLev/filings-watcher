"""Render EX-99 exhibits into classifier context, with volume control + metrics.

The exhibit (usually a press release) is the substance a thin Item 7.01/8.01
body just references. We feed it to the classifier as shared context — it may
shape all, some, or none of the items, so the model judges relevance (ADR 0031's
bounded-operator split: the LLM weighs the prose; deterministic code only bounds
volume and measures).

Volume is bounded by a character budget, applied across exhibits in order
(99.1 first), so any cut lands on the supplemental tail, never the primary
attachment. Crucially the cut is *measured, not silent*: `RenderedExhibits`
reports how much was dropped, and `scan_red_flags` lets a caller check whether
anything adverse sits in the dropped tail — so a filer can't quietly bury bad
news past the budget. We never prune for *relevance* in code (that is the LLM's
job); we only bound *length*.
"""

from __future__ import annotations

from typing import NamedTuple

from filings_orchestrator.edgar.document import FilingDocument

# Combined character budget for all exhibit text in one classification prompt.
# Sized so a normal press release fits whole (truncation is the exception, not
# the rule) while bounding token cost and dilution from long slide decks /
# financial tables. The full exhibit text is retained in storage regardless —
# this caps only what reaches the prompt.
_MAX_EXHIBIT_CHARS = 16_000

_EXHIBIT_PREAMBLE = (
    "--- Supplemental exhibits (EX-99.x) furnished with this filing ---\n"
    "These were attached by the filer. They may relate to all, some, or none of "
    "the item(s) above; use them as supporting context, not as separate items to "
    "classify."
)

# Curated high-severity adverse terms a filer might bury in a long exhibit.
# Deliberately an inclusion safety net over the *dropped* tail — not a relevance
# filter — so it is allowed to be imperfect (a net, not a guarantee). Lowercase
# substrings, matched case-insensitively. Phrased to limit false positives
# ("event of default", not bare "default"). Expanded as patterns are learned.
RED_FLAG_TERMS = frozenset(
    {
        "going concern",
        "material weakness",
        "restatement",
        "restate its",
        "event of default",
        "covenant breach",
        "breach of covenant",
        "acceleration of",
        "bankruptcy",
        "chapter 11",
        "delisting",
        "delist",
        "subpoena",
        "investigation",
        "sec inquiry",
        "fraud",
        "class action",
        "impairment charge",
    }
)


class RenderedExhibits(NamedTuple):
    """The exhibit prompt block plus volume metrics for one filing."""

    block: str  # labeled, budgeted text to append to the classifier prompt ("" if none)
    exhibit_count: int  # number of exhibits (named to avoid tuple.count)
    total_chars: int  # combined length of all exhibit text, untruncated
    used_chars: int  # characters actually placed in the prompt
    truncated: bool  # True if the budget cut any content
    dropped_chars: int  # characters omitted from the prompt
    dropped_text: str  # the omitted tail, for red-flag scanning


def render_exhibits(document: FilingDocument, budget: int = _MAX_EXHIBIT_CHARS) -> RenderedExhibits:
    """Build the budgeted exhibit context block and its volume metrics.

    Exhibits are consumed in `document.exhibits` order (99.1 first). Each
    contributes text until the shared budget is exhausted; the remainder and any
    later exhibits become `dropped_text`. Returns an empty block when the filing
    has no exhibits, leaving the prompt unchanged.
    """
    exhibits = document.exhibits
    total_chars = sum(len(ex.text) for ex in exhibits)
    if not exhibits:
        return RenderedExhibits("", 0, 0, 0, False, 0, "")

    parts: list[str] = [_EXHIBIT_PREAMBLE]
    dropped: list[str] = []
    remaining = budget
    used = 0
    for ex in exhibits:
        label = f"[{ex.exhibit_type} — {ex.document}]"
        if remaining <= 0:
            dropped.append(ex.text)
            continue
        if len(ex.text) <= remaining:
            parts.append(f"{label}\n{ex.text}")
            used += len(ex.text)
            remaining -= len(ex.text)
        else:
            parts.append(f"{label}\n{ex.text[:remaining]}")
            dropped.append(ex.text[remaining:])
            used += remaining
            remaining = 0

    dropped_chars = total_chars - used
    return RenderedExhibits(
        block="\n\n".join(parts),
        exhibit_count=len(exhibits),
        total_chars=total_chars,
        used_chars=used,
        truncated=dropped_chars > 0,
        dropped_chars=dropped_chars,
        dropped_text="\n".join(dropped),
    )


def scan_red_flags(text: str) -> list[str]:
    """Return the curated adverse terms present in `text` (case-insensitive)."""
    lowered = text.lower()
    return sorted(term for term in RED_FLAG_TERMS if term in lowered)
