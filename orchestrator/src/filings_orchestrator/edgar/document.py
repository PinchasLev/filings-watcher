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
from pydantic import BaseModel, Field

from filings_orchestrator.edgar.client import EdgarClient
from filings_orchestrator.edgar.models import Filing

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
    """A filing with its body text and per-Item sections."""

    filing: Filing
    text: str
    items: list[ItemSection] = Field(default_factory=list)
    raw_size_bytes: int


def fetch_filing_document(filing: Filing, client: EdgarClient) -> FilingDocument:
    """Fetch the primary document for a filing and return parsed text."""
    raw_html = client.get_text(filing.primary_document_url)
    text = _extract_plain_text(raw_html)
    items = _split_into_item_sections(text)
    return FilingDocument(
        filing=filing,
        text=text,
        items=items,
        raw_size_bytes=len(raw_html.encode("utf-8")),
    )


def _extract_plain_text(html: str) -> str:
    """Convert filing HTML to whitespace-normalized plain text.

    Strips script, style, and HTML comments; preserves block-level boundaries
    as newlines; collapses runs of whitespace.
    """
    soup = BeautifulSoup(html, "lxml")

    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

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
