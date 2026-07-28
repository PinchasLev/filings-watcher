"""Segment a periodic filing's Risk Factors (Item 1A) into whole risk-factor blocks.

Change-detection compares this year's disclosure to last year's. The comparable
*unit* is a whole risk factor, not a line or a sentence — a spike (ADR 0042) showed
line-level chunks produce noisy, boilerplate-dominated diffs while risk-factor-level
blocks isolate real changes. In a 10-K each risk factor is introduced by a **bold
header** (typically a full sentence), so we split Item 1A at bold headers.

Locating the section is the harder problem, because filers vary: some title the
heading "Item 1A. Risk Factors", some "Item 1A—Risk Factors" (an em/en-dash), and
some (Nike, McDonald's) put "Item 1A" only in the table of contents and title the real
heading just "Risk Factors"; some don't bold it at all (Intel, JPMorgan). So the
locator anchors on a bold "Risk Factors" heading (with an optional, dash-tolerant Item
1A prefix) and falls back to the specific "Item 1A … Risk Factors" text when no bold
heading exists, disambiguating the real section from the table of contents by span
size. A filing whose section can't be located, or whose extraction is degenerate,
yields no blocks — a coverage gap that is queryable (block_count = 0) and logged, not
a silent drop.

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

# The Risk Factors section opens with a heading that filers title variously: "Item
# 1A. Risk Factors", "Item 1A[dash]Risk Factors" (em/en-dash), or simply "Risk Factors" —
# several large filers (Nike, McDonald's) put "Item 1A" only in the table of contents
# and title the real section heading just "Risk Factors". So we anchor on the phrase
# "risk factors" at the start of a bold heading, with an OPTIONAL Item-1A prefix and a
# dash-tolerant separator, rather than on "Item 1A" text.
# Separators seen between "Item 1A" and "Risk Factors": period, colon, whitespace, and
# em/en-dash/hyphen (Costco writes "Item 1A[dash]Risk Factors"). Escaped to keep the source
# ASCII (ruff RUF001).
_SEP = r"[\s.:\u2014\u2013-]"
_RF_HEADING_RE = re.compile(rf"(?:item\s*1a\b{_SEP}*)?risk\s+factors\b")
# The more SPECIFIC anchor for the non-bold fallback: requires the "Item 1A" prefix, so
# it does not match the many inline "…these risk factors…" mentions in prose.
_ITEM1A_RF_RE = re.compile(rf"item\s*1a\b{_SEP}*risk\s+factors\b")
# The section closes at the next item — 1B (Unresolved Staff Comments), 1C
# (Cybersecurity), or 2 (Properties). Matched as text (for filers whose next heading
# isn't bold, e.g. Nike) OR as a bold heading by name (for filers who drop the item
# number from real headings, e.g. McDonald's "Properties").
_END_ITEM_RE = re.compile(rf"item[\s ]*(?:1b|1c|2){_SEP}")
_END_HEADING_RE = re.compile(
    r"(?:item\s*(?:1b|1c|2)\b|unresolved\s+staff|properties\b|legal\s+proceedings|mine\s+safety)"
)
# A located section shorter than this is a false locate — a table-of-contents entry
# (Item 1A → Item 1B a line apart) or an inline cross-reference — not the real section,
# which dwarfs it. Also the floor below which a parse is treated as degenerate.
_MIN_SECTION_CHARS = 1500


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


def _widest_span(starts: list[int], ends: list[int]) -> tuple[int, int] | None:
    """The widest (start -> nearest following end) at least `_MIN_SECTION_CHARS` long,
    or None. The size floor rejects table-of-contents entries and inline references
    (whose nearest end is a line away); the real section dwarfs them."""
    best: tuple[int, int] | None = None
    for s in starts:
        after = [e for e in ends if e > s]
        if not after:
            continue
        span = (s, min(after))
        if span[1] - span[0] < _MIN_SECTION_CHARS:
            continue
        if best is None or (span[1] - span[0]) > (best[1] - best[0]):
            best = span
    return best


def _locate_risk_factors(joined_lower: str, segments: list[_Segment]) -> tuple[int, int] | None:
    """Find the character span of the Risk Factors section, in two tiers.

    Section ends: the next item (1B/1C/2) as text, or a bold next-section heading by
    name (for filers who drop the item number from real headings, e.g. McDonald's
    "Properties").

    1. Primary: BOLD heading segments whose text *begins* with the risk-factors pattern.
       Requiring bold + start-of-segment excludes both the (usually non-bold) TOC entry
       and inline references ("see Item 1A. Risk Factors"), which sit mid-paragraph.
    2. Fallback (filers who do not bold the heading, e.g. Intel, JPMorgan): anchor on the
       specific "Item 1A ... Risk Factors" text — specific enough not to match the many
       inline "these risk factors" mentions — and take the widest valid span.
    """
    ends = [m.start() for m in _END_ITEM_RE.finditer(joined_lower)]
    ends += [
        seg.start
        for seg in segments
        if seg.bold and _END_HEADING_RE.match(seg.text.strip().lower())
    ]
    ends.sort()

    bold_starts = [
        seg.start for seg in segments if seg.bold and _RF_HEADING_RE.match(seg.text.strip().lower())
    ]
    span = _widest_span(bold_starts, ends)
    if span is not None:
        return span

    ref_starts = [m.start() for m in _ITEM1A_RF_RE.finditer(joined_lower)]
    return _widest_span(ref_starts, ends)


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
    span = _locate_risk_factors(joined_lower, segments)
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
    # Degenerate-parse guard: if the surviving content is implausibly small for a Risk
    # Factors section, treat it as a failed extraction and emit nothing — better a
    # visibly-absent filing (queryable via block_count = 0) than a misleading headline
    # synthesized from a fragment (the Carnival 2-block case).
    if sum(len(b.text) for b in blocks) < _MIN_SECTION_CHARS:
        return []
    return blocks
