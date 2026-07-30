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
locator works in three tiers: (1) a bold "Risk Factors" heading (with an optional,
dash-tolerant Item 1A prefix); (2) the specific "Item 1A … Risk Factors" text when no
bold heading exists — both disambiguating the real section from the table of contents by
span size; and (3) for modern inline-XBRL filings (Intel, Microsoft, most large filers)
whose heading is a CSS-styled table row — non-bold, with "Item 1A" and "Risk Factors" in
separate cells and even a fragmented word ("Ris k Factors") — the filing's own table-of-
contents anchor links, whose targets bound the section. A filing whose section can't be
located by any tier, or whose extraction is degenerate, yields no blocks — a coverage gap
that is queryable (block_count = 0) and logged, not a silent drop.

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

# The high-end degenerate guard. When the section's END markers (Item 1B/1C/2) are not
# detected — e.g. an inline-XBRL filing that fragments those headings across table cells,
# as it does the start — the span runs past Item 1A to a far-off later heading, swallowing
# MD&A, financials, and controls (Morgan Stanley's 2025 10-K over-captured to 675k chars /
# 646 blocks, its last block being Item 9A). A real Risk Factors section, even a large
# bank's, is far smaller; a parse this size is an over-capture, so suppress it (0 blocks,
# queryable) rather than emit blocks that would diff into nonsense. Generous, so a
# genuinely large section is never suppressed. Correctly locating such a section's end is
# the same fragmentation-tolerant-heading work as the opaque-anchor residual.
_MAX_SECTION_CHARS = 400_000


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

    joined_lower = "\n".join(text for _, text in runs).lower()
    return _segments_from_runs(runs), joined_lower


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


def _segments_from_runs(runs: list[tuple[bool, str]]) -> list[_Segment]:
    """Build offset-carrying segments from (bold, text) runs — the tail shared by the
    coalescer and the anchor locator."""
    segments: list[_Segment] = []
    pos = 0
    for bold, text in runs:
        segments.append(_Segment(bold=bold, text=text, start=pos, end=pos + len(text)))
        pos += len(text) + 1
    return segments


def _find_by_anchor(soup: BeautifulSoup, target: str) -> Tag | None:
    """The element a TOC link points at — by id, or the older <a name="..."> form."""
    hit = soup.find(id=target) or soup.find(attrs={"name": target})
    return hit if isinstance(hit, Tag) else None


def _locate_via_toc_anchors(html: str) -> list[_Segment] | None:
    """Third-tier locator for inline-XBRL filings whose Risk Factors heading is neither
    bold nor contiguous in text — Intel, Microsoft, and most large filers now render
    section headings as CSS-styled table rows (not <b>), split "Item 1A" and "Risk
    Factors" across cells, and even fragment a word ("Ris k Factors") via tagged spans,
    so both text tiers miss the start. Those filings carry an auto-generated table of
    contents of in-page links, so we locate the section by its own anchor: the TOC link
    whose text is "Risk Factors" points at the section's start id, and the next section's
    link ("Unresolved Staff Comments" / "Item 1B" / …) bounds its end. We then walk the
    DOM between the two, preserving each string's boldness so bold sub-headers (the
    individual risk factors) still drive block splitting.

    Returns None when no usable pair of TOC anchors resolves — a safe degrade to the same
    no-blocks outcome as before, never a change to the healthy text-tier path.
    """
    soup = BeautifulSoup(html, "html.parser")
    for el in soup(["script", "style"]):
        el.decompose()

    rf_target: str | None = None
    end_target: str | None = None
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not isinstance(href, str) or not href.startswith("#") or len(href) < 2:
            continue
        text = _normalize_ws(a.get_text()).lower()
        if rf_target is None:
            if _RF_HEADING_RE.match(text):
                rf_target = href[1:]
        elif _END_HEADING_RE.match(text):
            end_target = href[1:]
            break
    if rf_target is None or end_target is None:
        return None

    start_el = _find_by_anchor(soup, rf_target)
    end_el = _find_by_anchor(soup, end_target)
    if start_el is None or end_el is None:
        return None

    runs: list[tuple[bool, str]] = []
    for node in start_el.next_elements:
        if node is end_el:
            break
        if not isinstance(node, NavigableString):
            continue
        text = _normalize_ws(str(node))
        if not text:
            continue
        bold = _is_bold(node)
        if runs and runs[-1][0] == bold:
            runs[-1] = (bold, runs[-1][1] + " " + text)
        else:
            runs.append((bold, text))
    return _segments_from_runs(runs)


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
    if span is not None:
        in_span = _segments_in_span(segments, span)
    else:
        # Third tier: inline-XBRL filings (Intel, Microsoft, …) whose heading is neither
        # bold nor contiguous in text. Located via the filing's own table-of-contents
        # anchors. Only reached when both text tiers fail, so the healthy path is
        # unchanged; None here degrades to the same no-blocks outcome as before.
        anchored = _locate_via_toc_anchors(html)
        if anchored is None:
            return []
        in_span = anchored

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
    # Degenerate-parse guard, both ends: emit nothing (block_count = 0, queryable) rather
    # than misleading blocks when the surviving content is implausibly SMALL for a Risk
    # Factors section (a false locate — the Carnival 2-block case) or implausibly LARGE (an
    # over-capture past undetected end markers — the Morgan Stanley Item-9A case). Better a
    # visibly-absent filing than a headline synthesized from a fragment or from half the 10-K.
    total_chars = sum(len(b.text) for b in blocks)
    if total_chars < _MIN_SECTION_CHARS or total_chars > _MAX_SECTION_CHARS:
        return []
    return blocks
