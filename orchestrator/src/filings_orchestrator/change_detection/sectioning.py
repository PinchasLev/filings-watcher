"""Segment a periodic filing's Risk Factors (Item 1A) into whole risk-factor blocks.

Change-detection compares this year's disclosure to last year's. The comparable
*unit* is a whole risk factor, not a line or a sentence — a spike (ADR 0042) showed
line-level chunks produce noisy, boilerplate-dominated diffs while risk-factor-level
blocks isolate real changes. In a 10-K each risk factor is introduced by a **bold
header** (typically a full sentence), so we split Item 1A at bold headers.

Filers format their HTML differently, so this is best-effort with a fallback: when
no usable bold-header structure is found (older or oddly-formatted filings), we fall
back to merging paragraphs into fixed-size blocks. Either way each block carries a
stable identity hash (over its whitespace-normalized text) so a later diff can detect
verbatim carry-over cheaply and key a block across periods.

Deterministic and offline: HTML in, blocks out. No network, no database, no LLM.
"""

from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag
from pydantic import BaseModel

# A bold segment must be at least this long to count as a risk-factor header,
# rather than an inline bold word used for emphasis mid-paragraph. Real risk-factor
# headers are full sentences; single bolded words/terms fall well under this.
_MIN_HEADER_CHARS = 25

# Blocks shorter than this are dropped: stray page numbers, one-line sub-headers,
# and fragments that carry no standalone risk content.
_MIN_BLOCK_CHARS = 120

# If header-splitting finds fewer than this many headers, the filing has no usable
# bold structure and we use the size-merge fallback instead.
_MIN_HEADERS_FOR_STRUCTURE = 2

# Target block size for the fallback path, in characters — a few paragraphs, roughly
# one risk factor's worth.
_FALLBACK_TARGET_CHARS = 1800

# CSS font-weight values (and named weights) that render as bold.
_BOLD_WEIGHTS = frozenset({"bold", "bolder", "600", "700", "800", "900"})
_FONT_WEIGHT_RE = re.compile(r"font-weight\s*:\s*(\w+)")

# Item 1A opens the Risk Factors section; Item 1B (Unresolved Staff Comments) or
# Item 2 (Properties) closes it. Both appear twice in a 10-K — once in the table of
# contents, once as the real heading — so we pick the widest span (see _locate).
_ITEM_1A_RE = re.compile(r"item[\s ]*1a[\.\:\s]")
_ITEM_END_RE = re.compile(r"item[\s ]*(?:1b|2)[\.\:\s]")


class RiskFactorBlock(BaseModel):
    """One risk factor (or fallback block) extracted from Item 1A.

    `text` is whitespace-normalized. `heading` is the bold header that opened the
    block, or None for the section intro and for fallback blocks. `block_hash` is a
    stable identity over the normalized text — equal iff the text is verbatim-equal,
    so a later diff can detect carry-over and key the block across periods.
    """

    index: int
    heading: str | None
    text: str
    block_hash: str


class _Segment(BaseModel):
    """A run of same-boldness text, with its offsets in the joined document string."""

    bold: bool
    text: str
    start: int
    end: int


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _block_hash(normalized_text: str) -> str:
    return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()


def _is_bold(node: NavigableString) -> bool:
    """True if any ancestor renders `node` bold (a <b>/<strong>/<hN> tag or a
    font-weight style of 600+)."""
    for parent in node.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name in ("b", "strong", "h1", "h2", "h3", "h4"):
            return True
        style = parent.get("style")
        if isinstance(style, str):
            m = _FONT_WEIGHT_RE.search(style)
            if m and m.group(1).lower() in _BOLD_WEIGHTS:
                return True
    return False


def _coalesced_segments(html: str) -> tuple[list[_Segment], str]:
    """Flatten the document into runs of same-boldness text, coalescing adjacent
    runs. Returns the segments (with offsets) and the joined lowercased text used to
    locate section boundaries."""
    soup = BeautifulSoup(html, "html.parser")
    for el in soup(["script", "style"]):
        el.decompose()

    runs: list[tuple[bool, str]] = []
    for string in soup.strings:
        text = _normalize_ws(str(string))
        if not text:
            continue
        bold = _is_bold(string)
        if runs and runs[-1][0] == bold:
            runs[-1] = (bold, runs[-1][1] + " " + text)
        else:
            runs.append((bold, text))

    segments: list[_Segment] = []
    pos = 0
    for bold, text in runs:
        segments.append(_Segment(bold=bold, text=text, start=pos, end=pos + len(text)))
        pos += len(text) + 1  # +1 for the "\n" join below
    joined_lower = "\n".join(text for _, text in runs).lower()
    return segments, joined_lower


def _locate_item_1a(joined_lower: str) -> tuple[int, int] | None:
    """Find the character span of the Risk Factors section: from an "Item 1A" to the
    next "Item 1B"/"Item 2" after it. Picks the widest such span so the real section
    wins over the short table-of-contents entry."""
    starts = [m.start() for m in _ITEM_1A_RE.finditer(joined_lower)]
    ends = [m.start() for m in _ITEM_END_RE.finditer(joined_lower)]
    best: tuple[int, int] | None = None
    for s in starts:
        after = [e for e in ends if e > s]
        if not after:
            continue
        span = (s, min(after))
        if best is None or (span[1] - span[0]) > (best[1] - best[0]):
            best = span
    return best


def _segments_in_span(segments: list[_Segment], span: tuple[int, int]) -> list[_Segment]:
    """Return the segments overlapping the span, each clipped to the overlap. Clipping
    (not just selecting) matters when a filing has no bold structure: everything then
    coalesces into one segment, and only clipping trims the section's boundaries out
    of it."""
    start, end = span
    clipped: list[_Segment] = []
    for seg in segments:
        lo, hi = max(seg.start, start), min(seg.end, end)
        if hi <= lo:
            continue
        text = seg.text[lo - seg.start : hi - seg.start].strip()
        if text:
            clipped.append(_Segment(bold=seg.bold, text=text, start=lo, end=hi))
    return clipped


def _split_on_headers(segments: list[_Segment]) -> list[tuple[str | None, str]]:
    """Group segments into blocks, starting a new block at each bold header. A header
    is a bold segment of at least `_MIN_HEADER_CHARS`. Text before the first header
    (the section intro) becomes a headingless block."""
    blocks: list[tuple[str | None, str]] = []
    heading: str | None = None
    buffer: str | None = None
    for seg in segments:
        if seg.bold and len(seg.text) >= _MIN_HEADER_CHARS:
            if buffer is not None:
                blocks.append((heading, buffer))
            heading, buffer = seg.text, seg.text
        elif buffer is None:
            heading, buffer = None, seg.text
        else:
            buffer += " " + seg.text
    if buffer is not None:
        blocks.append((heading, buffer))
    return blocks


def _size_merge_fallback(segments: list[_Segment]) -> list[tuple[str | None, str]]:
    """Merge segments into fixed-size blocks — used when no bold-header structure is
    found. Blocks are headingless."""
    blocks: list[tuple[str | None, str]] = []
    buffer = ""
    for seg in segments:
        buffer = (buffer + " " + seg.text).strip() if buffer else seg.text
        if len(buffer) >= _FALLBACK_TARGET_CHARS:
            blocks.append((None, buffer))
            buffer = ""
    if buffer:
        blocks.append((None, buffer))
    return blocks


def segment_risk_factors(html: str) -> list[RiskFactorBlock]:
    """Segment a 10-K's Item 1A into whole risk-factor blocks.

    Returns an empty list when no Risk Factors section can be located. Splits at bold
    headers; falls back to size-merged blocks when the filing has no usable header
    structure. Blocks shorter than `_MIN_BLOCK_CHARS` are dropped.
    """
    segments, joined_lower = _coalesced_segments(html)
    span = _locate_item_1a(joined_lower)
    if span is None:
        return []

    in_span = _segments_in_span(segments, span)
    header_blocks = _split_on_headers(in_span)
    header_count = sum(1 for heading, _ in header_blocks if heading is not None)
    raw_blocks = (
        header_blocks
        if header_count >= _MIN_HEADERS_FOR_STRUCTURE
        else _size_merge_fallback(in_span)
    )

    blocks: list[RiskFactorBlock] = []
    for heading, text in raw_blocks:
        normalized = _normalize_ws(text)
        if len(normalized) < _MIN_BLOCK_CHARS:
            continue
        blocks.append(
            RiskFactorBlock(
                index=len(blocks),
                heading=_normalize_ws(heading) if heading else None,
                text=normalized,
                block_hash=_block_hash(normalized),
            )
        )
    return blocks
