"""Tests for the filing document fetch and HTML parsing.

Uses a synthetic 8-K HTML fixture; no live network in CI.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import respx

from filings_orchestrator.edgar import EdgarClient, fetch_filing_document
from filings_orchestrator.edgar.document import (
    _extract_plain_text,
    _split_into_item_sections,
)
from filings_orchestrator.edgar.models import Filing

FIXTURES = Path(__file__).parent / "fixtures"


def _sample_filing() -> Filing:
    return Filing(
        cik="0000320193",
        company_name="Apple Inc.",
        ticker="AAPL",
        form="8-K",
        accession_number="0000320193-26-000045",
        filing_date=date(2026, 4, 30),
        report_date=date(2026, 4, 30),
        primary_document="aapl-20260430.htm",
        primary_document_url=(
            "https://www.sec.gov/Archives/edgar/data/320193/000032019326000045/aapl-20260430.htm"
        ),
    )


def test_extract_plain_text_strips_scripts_styles_and_comments() -> None:
    html = (FIXTURES / "sample_8k.html").read_text()
    text = _extract_plain_text(html)
    assert "noise" not in text  # script body removed
    assert "font-family" not in text  # style body removed
    assert "EDGAR-generated comment" not in text  # HTML comment removed


def test_extract_plain_text_preserves_filing_substance() -> None:
    html = (FIXTURES / "sample_8k.html").read_text()
    text = _extract_plain_text(html)
    assert "Item 2.02 Results of Operations and Financial Condition." in text
    assert "Item 9.01 Financial Statements and Exhibits." in text
    assert "press release" in text


def test_extract_plain_text_collapses_blank_runs() -> None:
    html = "<html><body><p>A</p>\n\n\n\n<p>B</p></body></html>"
    text = _extract_plain_text(html)
    assert "\n\n\n" not in text


def test_split_into_item_sections_finds_each_item() -> None:
    text = _extract_plain_text((FIXTURES / "sample_8k.html").read_text())
    sections = _split_into_item_sections(text)
    numbers = [s.number for s in sections]
    assert numbers == ["2.02", "9.01"]


def test_split_into_item_sections_captures_titles() -> None:
    text = _extract_plain_text((FIXTURES / "sample_8k.html").read_text())
    sections = _split_into_item_sections(text)
    assert sections[0].title is not None
    assert "Results of Operations" in sections[0].title
    assert sections[1].title is not None
    assert "Financial Statements" in sections[1].title


def test_split_into_item_sections_captures_body_between_headings() -> None:
    text = _extract_plain_text((FIXTURES / "sample_8k.html").read_text())
    sections = _split_into_item_sections(text)
    assert "press release" in sections[0].text
    assert "Exhibits" in sections[1].text or "99.1" in sections[1].text


def test_split_into_item_sections_returns_empty_when_no_headings() -> None:
    text = "Just some prose without any item headings at all."
    assert _split_into_item_sections(text) == []


def test_fetch_filing_document_end_to_end() -> None:
    """fetch_filing_document calls the client, parses HTML, returns structured doc."""
    html = (FIXTURES / "sample_8k.html").read_text()
    filing = _sample_filing()

    with respx.mock(assert_all_called=True) as mock:
        mock.get(filing.primary_document_url).mock(return_value=httpx.Response(200, text=html))
        with EdgarClient(user_agent="filings-watcher tester@example.com") as client:
            document = fetch_filing_document(filing, client)

    assert document.filing.accession_number == filing.accession_number
    assert document.raw_size_bytes > 0
    assert len(document.text) > 0
    assert [s.number for s in document.items] == ["2.02", "9.01"]
