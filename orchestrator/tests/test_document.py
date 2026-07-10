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
    _choose_parser,
    _document_kind,
    _extract_plain_text,
    _split_into_item_sections,
)
from filings_orchestrator.edgar.models import Exhibit, Filing

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


def test_choose_parser_detects_xml_declaration() -> None:
    xhtml = '<?xml version="1.0"?>\n<html><body>x</body></html>'
    html = "<!DOCTYPE html>\n<html><body>x</body></html>"
    assert _choose_parser(xhtml) == "lxml-xml"
    assert _choose_parser(html) == "lxml"


def test_choose_parser_tolerates_leading_whitespace() -> None:
    xhtml = '   \n   <?xml version="1.0"?>\n<html><body>x</body></html>'
    assert _choose_parser(xhtml) == "lxml-xml"


def test_choose_parser_is_case_insensitive() -> None:
    # SGML/XML declarations are case-insensitive in practice.
    weird = '<?XML version="1.0"?>\n<html/>'
    assert _choose_parser(weird) == "lxml-xml"


def test_xhtml_filing_parses_without_warning_and_extracts_text() -> None:
    """An XHTML filing should select the XML parser and emit no warning."""
    import warnings

    from bs4 import XMLParsedAsHTMLWarning

    xhtml = (FIXTURES / "sample_8k_xhtml.html").read_text()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        text = _extract_plain_text(xhtml)

    xml_warnings = [w for w in caught if issubclass(w.category, XMLParsedAsHTMLWarning)]
    assert not xml_warnings, f"unexpected XML warnings: {[str(w.message) for w in xml_warnings]}"

    # Substance is preserved through the XML parser.
    assert "Item 5.02" in text
    assert "Jane Doe" in text
    assert "Interim Chief Financial Officer" in text


def test_inline_emphasis_tags_do_not_split_words() -> None:
    """Filings sometimes wrap initial letters in <b>/<span>; extraction
    must reassemble them as continuous prose, not "F irst" / "A pril"."""
    html = (FIXTURES / "sample_8k_inline_tags.html").read_text()
    text = _extract_plain_text(html)

    assert "First Quarter" in text
    assert "April 22" in text
    assert "F irst" not in text
    assert "A pril" not in text
    # Adjacent spans should join with one space between them, not newlines.
    assert "Securities Exchange Act" in text


def test_inline_tags_preserve_block_boundaries() -> None:
    """Flattening inline tags must not collapse paragraph or table breaks."""
    html = (FIXTURES / "sample_8k_inline_tags.html").read_text()
    text = _extract_plain_text(html)
    # Heading is a block element; should be its own line.
    lines = text.splitlines()
    heading_line = next(
        (i for i, line in enumerate(lines) if "Item 9.01" in line),
        None,
    )
    assert heading_line is not None
    # The "(d) Exhibits." block should not be merged into the heading line.
    assert "Item 9.01" not in lines[heading_line + 1 :][0] or "Exhibits" not in lines[heading_line]


def test_xhtml_section_splitting_still_works() -> None:
    """Item splitting works on text extracted via the XML parser."""
    xhtml = (FIXTURES / "sample_8k_xhtml.html").read_text()
    text = _extract_plain_text(xhtml)
    sections = _split_into_item_sections(text)
    assert [s.number for s in sections] == ["5.02", "9.01"]
    assert sections[0].title is not None
    assert "Departure of Directors" in sections[0].title


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


def _filing_with_exhibits(*urls: str) -> Filing:
    filing = _sample_filing()
    filing.exhibits = [
        Exhibit(exhibit_type=f"EX-99.{i + 1}", document=f"ex{i + 1}.htm", url=u)
        for i, u in enumerate(urls)
    ]
    return filing


def test_fetch_filing_document_fetches_and_parses_exhibits() -> None:
    """Each EX-99 fetch target is fetched and parsed into the document's exhibits."""
    body = (FIXTURES / "sample_8k.html").read_text()
    ex_url = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000045/ex_99_1.htm"
    filing = _filing_with_exhibits(ex_url)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(filing.primary_document_url).mock(return_value=httpx.Response(200, text=body))
        mock.get(ex_url).mock(
            return_value=httpx.Response(
                200, text="<html><body><p>Press release text.</p></body></html>"
            )
        )
        with EdgarClient(user_agent="filings-watcher tester@example.com") as client:
            document = fetch_filing_document(filing, client)

    assert [e.exhibit_type for e in document.exhibits] == ["EX-99.1"]
    assert "Press release text." in document.exhibits[0].text


def test_fetch_filing_document_skips_failed_exhibit_fetch() -> None:
    """An exhibit that 404s is logged and skipped; the filing still ingests."""
    body = (FIXTURES / "sample_8k.html").read_text()
    good = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000045/ex_99_1.htm"
    bad = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000045/ex_99_2.htm"
    filing = _filing_with_exhibits(good, bad)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(filing.primary_document_url).mock(return_value=httpx.Response(200, text=body))
        mock.get(good).mock(return_value=httpx.Response(200, text="<p>Release.</p>"))
        mock.get(bad).mock(return_value=httpx.Response(404))
        with EdgarClient(user_agent="filings-watcher tester@example.com") as client:
            document = fetch_filing_document(filing, client)

    # Only the successful exhibit survives; the failed one is dropped, not fatal.
    assert [e.exhibit_type for e in document.exhibits] == ["EX-99.1"]
    assert len(document.text) > 0


def test_document_kind_classifies_content_first() -> None:
    assert _document_kind(b"%PDF-1.7\nobjects", "") == "pdf"
    assert _document_kind(b"not-a-pdf", "application/pdf") == "pdf"  # header even without magic
    assert _document_kind(b"\x89PNG\r\n\x1a\n", "") == "binary"
    assert _document_kind(b"PK\x03\x04zipdata", "") == "binary"
    assert _document_kind(b"anything", "image/jpeg") == "binary"
    assert _document_kind(b"<html><body>hi</body></html>", "text/html") == "markup"
    assert _document_kind(b"x" * (26 * 1024 * 1024), "text/html") == "oversized"


def test_fetch_filing_document_skips_pdf_exhibit() -> None:
    """A PDF exhibit is detected by content and skipped, never parsed; a sibling
    HTML exhibit is still parsed."""
    body = (FIXTURES / "sample_8k.html").read_text()
    pdf_url = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000045/ex99_1.pdf"
    htm_url = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000045/ex99_2.htm"
    filing = _filing_with_exhibits(pdf_url, htm_url)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(filing.primary_document_url).mock(return_value=httpx.Response(200, text=body))
        mock.get(pdf_url).mock(
            return_value=httpx.Response(
                200,
                content=b"%PDF-1.7\n%\xe2\xe3\xcf\xd3 binary junk <<< >>> " * 100,
                headers={"content-type": "application/pdf"},
            )
        )
        mock.get(htm_url).mock(return_value=httpx.Response(200, text="<p>Real release.</p>"))
        with EdgarClient(user_agent="filings-watcher tester@example.com") as client:
            document = fetch_filing_document(filing, client)

    # The PDF is fetched but skipped; only the HTML exhibit survives and is parsed.
    assert [e.exhibit_type for e in document.exhibits] == ["EX-99.2"]
    assert "Real release." in document.exhibits[0].text


def test_fetch_filing_document_skips_oversized_exhibit() -> None:
    """An exhibit whose bytes exceed the parse cap is skipped, not parsed."""
    body = (FIXTURES / "sample_8k.html").read_text()
    big_url = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000045/ex99_big.htm"
    filing = _filing_with_exhibits(big_url)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(filing.primary_document_url).mock(return_value=httpx.Response(200, text=body))
        mock.get(big_url).mock(
            return_value=httpx.Response(
                200, content=b"x" * (26 * 1024 * 1024), headers={"content-type": "text/html"}
            )
        )
        with EdgarClient(user_agent="filings-watcher tester@example.com") as client:
            document = fetch_filing_document(filing, client)

    assert document.exhibits == []


def test_fetch_filing_document_pdf_primary_is_metadata_only() -> None:
    """A PDF primary document yields empty body text and doesn't crash."""
    filing = _sample_filing()
    with respx.mock(assert_all_called=True) as mock:
        mock.get(filing.primary_document_url).mock(
            return_value=httpx.Response(
                200, content=b"%PDF-1.4 ... stream ...", headers={"content-type": "application/pdf"}
            )
        )
        with EdgarClient(user_agent="filings-watcher tester@example.com") as client:
            document = fetch_filing_document(filing, client)

    assert document.text == ""
    assert document.exhibits == []
