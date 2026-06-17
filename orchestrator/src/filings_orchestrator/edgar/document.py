"""Fetch and parse the body of an 8-K filing document.

The metadata feed (`filings.py`) gives us which filings exist and what Item
numbers each one discloses. The classifier needs the prose under each Item.
This module fetches the filing's primary document (HTML) and produces:

- `text`: a whitespace-normalized plain-text view of the whole filing
- `items`: per-Item sections, split by heading where they can be located

The split is best-effort. Real 8-K HTML is heterogeneous (inline XBRL,
nested tables, varying heading styles). When section splitting fails for a
given filing, callers still have `text` for the full body.
"""

from __future__ import annotations

import re
from typing import cast

from bs4 import BeautifulSoup, Comment
from bs4.element import NavigableString
from pydantic import BaseModel, Field

from filings_orchestrator.edgar.client import EdgarClient
from filings_orchestrator.edgar.models import Exhibit, Filing
from filings_orchestrator.log_events import emit

# Inline HTML elements: their text continues a line of prose and must not
# be separated from surrounding text by newlines. Filings sometimes wrap
# single letters in these for visual styling (drop caps, font tweaks);
# letting `get_text(separator="\n")` split on each one produces output
# like "F irst" instead of "First".
_INLINE_TAGS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "cite",
        "code",
        "del",
        "em",
        "font",
        "i",
        "ins",
        "kbd",
        "mark",
        "q",
        "s",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "time",
        "tt",
        "u",
        "var",
    }
)

# Match a line that opens an Item section. Captures the dotted number
# ("5.02") and any inline title text that follows on the same line.
# Tolerates common variants: "Item 5.02", "ITEM 5.02.", "Item 5.02 -",
# "Item 5.02 — Departure of Directors...".
# The character class allows: U+2013 (EN DASH), U+2014 (EM DASH), and ASCII
# hyphen / colon / period. These appear as layout separators in filings
# between the Item number and its title; matching them tolerates the
# common heading styles without anchoring on one specific punctuation.
_ITEM_HEADING_RE = re.compile(
    r"^\s*item\s+(\d+\.\d+)\.?\s*[\u2013\u2014\-:.]?\s*(.*?)$",
    re.IGNORECASE,
)


class ItemSection(BaseModel):
    """Prose disclosed under one Item of an 8-K."""

    number: str
    title: str | None = None
    text: str


class FilingDocument(BaseModel):
    """A filing with its body text, per-Item sections, and EX-99 exhibits."""

    filing: Filing
    text: str
    items: list[ItemSection] = Field(default_factory=list)
    exhibits: list[Exhibit] = Field(default_factory=list)
    raw_size_bytes: int


def fetch_filing_document(filing: Filing, client: EdgarClient) -> FilingDocument:
    """Fetch the primary document and EX-99 exhibits, return parsed text.

    Each exhibit on `filing.exhibits` (a fetch target from the resolver) is
    fetched and parsed into plain text. An exhibit fetch failure is logged and
    skipped, not raised: exhibits are supplemental context, so one missing
    attachment must not abort ingestion of the filing itself.
    """
    raw_html = client.get_text(filing.primary_document_url)
    text = _extract_plain_text(raw_html)
    items = _split_into_item_sections(text)

    exhibits: list[Exhibit] = []
    for ref in filing.exhibits:
        try:
            exhibit_html = client.get_text(ref.url)
        except Exception as exc:  # supplemental — skip this one, keep the filing
            emit(
                "exhibit_fetch_failed",
                accession_number=filing.accession_number,
                exhibit_type=ref.exhibit_type,
                url=ref.url,
                error_class=type(exc).__name__,
            )
            continue
        exhibits.append(
            Exhibit(
                exhibit_type=ref.exhibit_type,
                document=ref.document,
                url=ref.url,
                text=_extract_plain_text(exhibit_html),
            )
        )

    return FilingDocument(
        filing=filing,
        text=text,
        items=items,
        exhibits=exhibits,
        raw_size_bytes=len(raw_html.encode("utf-8")),
    )


def _extract_plain_text(html: str) -> str:
    """Convert filing markup to whitespace-normalized plain text.

    Strips script, style, and comment nodes; flattens inline emphasis
    tags so their content reads as continuous prose rather than splitting
    surrounding words; preserves block-level boundaries as newlines;
    collapses runs of whitespace. Selects the parser based on the
    document's declared shape (see `_choose_parser`).
    """
    soup = BeautifulSoup(html, _choose_parser(html))

    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    # Flatten inline tags into NavigableStrings, then smooth so adjacent
    # strings merge. Without this, get_text(separator="\n") would split
    # text across every inline boundary (e.g., "F" + "irst" becomes
    # "F\nirst" when the source HTML is "<b>F</b>irst").
    for tag in list(soup.find_all(_INLINE_TAGS)):
        tag.replace_with(NavigableString(tag.get_text()))
    soup.smooth()

    raw = soup.get_text(separator="\n")
    # Normalize whitespace: collapse runs of spaces/tabs/NBSP and limit
    # blank lines to at most one between paragraphs.
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in raw.splitlines()]
    normalized: list[str] = []
    blank_streak = 0
    for line in lines:
        if not line:
            blank_streak += 1
            if blank_streak <= 1:
                normalized.append("")
        else:
            blank_streak = 0
            normalized.append(line)
    return "\n".join(normalized).strip()


def _choose_parser(raw: str) -> str:
    """Pick the BeautifulSoup parser appropriate for the document shape.

    EDGAR filings are served as a mix of plain HTML and XHTML (the XML-
    conformant variant of HTML, identified by a leading `<?xml ... ?>`
    declaration). Using `lxml-xml` on XHTML avoids a misparse warning and
    preserves namespaced elements; using `lxml` on plain HTML is more
    tolerant of the malformed markup common in older filings.
    """
    leading = raw.lstrip()[:256].lower()
    if leading.startswith("<?xml"):
        return "lxml-xml"
    return "lxml"


def _split_into_item_sections(text: str) -> list[ItemSection]:
    """Split filing text into per-Item sections by detecting heading lines.

    Returns an empty list when no Item headings can be located, which happens
    for filings whose HTML doesn't render Item headers as distinct lines.
    """
    lines = text.splitlines()
    headings: list[tuple[int, str, str]] = []
    for idx, line in enumerate(lines):
        match = _ITEM_HEADING_RE.match(line)
        if match:
            number = match.group(1)
            title = match.group(2).strip() or None
            headings.append((idx, number, cast("str", title) if title else ""))

    if not headings:
        return []

    sections: list[ItemSection] = []
    for i, (line_idx, number, title) in enumerate(headings):
        end_idx = headings[i + 1][0] if i + 1 < len(headings) else len(lines)
        body_lines = lines[line_idx + 1 : end_idx]
        body = "\n".join(body_lines).strip()
        sections.append(
            ItemSection(
                number=number,
                title=title or None,
                text=body,
            )
        )
    return sections
