"""Tests for the shared filing resolver used by both ingest paths."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from filings_orchestrator.edgar import EdgarClient
from filings_orchestrator.edgar.filing_resolver import (
    _extract_exhibit_99_refs,
    _extract_primary_document_name,
    _to_date,
    resolve_filing,
)

FIXTURES = Path(__file__).parent / "fixtures"
_SAMPLE_FILING_INDEX_HTML = (FIXTURES / "filing_index_8k.html").read_text()

# A filing-index table carrying several EX-99 exhibits out of attachment order,
# plus a non-EX-99 exhibit (EX-10.1) that must be ignored.
_MULTI_EXHIBIT_INDEX_HTML = """
<table summary="Document Format Files">
  <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
  <tr><td>1</td><td>FORM 8-K</td>
      <td><a href="/Archives/edgar/data/5/000000000000000005/f8k.htm">f8k.htm</a></td>
      <td>8-K</td><td>100</td></tr>
  <tr><td>3</td><td>EXHIBIT 99.2</td>
      <td><a href="/Archives/edgar/data/5/000000000000000005/ex992.htm">ex992.htm</a></td>
      <td>EX-99.2</td><td>200</td></tr>
  <tr><td>2</td><td>PRESS RELEASE</td>
      <td><a href="/Archives/edgar/data/5/000000000000000005/ex991.htm">ex991.htm</a></td>
      <td>EX-99.1</td><td>300</td></tr>
  <tr><td>4</td><td>MATERIAL CONTRACT</td>
      <td><a href="/Archives/edgar/data/5/000000000000000005/ex101.htm">ex101.htm</a></td>
      <td>EX-10.1</td><td>400</td></tr>
</table>
"""


def test_extract_primary_document_strips_ixbrl_viewer_prefix() -> None:
    name = _extract_primary_document_name(_SAMPLE_FILING_INDEX_HTML, "8-K")
    assert name == "f8k_051426.htm"


def test_extract_primary_document_skips_non_matching_rows() -> None:
    """The function must return the 8-K row's document, NOT the EX-99.1 row's,
    even though both are .htm files in the same table."""
    name = _extract_primary_document_name(_SAMPLE_FILING_INDEX_HTML, "8-K")
    assert name != "ex_99_1.htm"


def test_extract_primary_document_raises_when_form_not_found() -> None:
    with pytest.raises(LookupError, match="no Document Format Files row"):
        _extract_primary_document_name(_SAMPLE_FILING_INDEX_HTML, "10-K")


def test_resolve_filing_fetches_index_html_and_builds_filing_from_daily_index_fields() -> None:
    """The daily-index path passes the compact YYYYMMDD `filed_at` shape."""
    index_url = (
        "https://www.sec.gov/Archives/edgar/data/101295/000117184326003455/"
        "0001171843-26-003455-index.html"
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.get(index_url).mock(return_value=httpx.Response(200, text=_SAMPLE_FILING_INDEX_HTML))
        with EdgarClient(user_agent="filings-watcher tester@example.com") as client:
            filing = resolve_filing(
                cik="0000101295",
                accession_number="0001171843-26-003455",
                company_name="UNITED GUARDIAN INC",
                form="8-K",
                filed_at="20260515",
                client=client,
            )

    assert filing.accession_number == "0001171843-26-003455"
    assert filing.cik == "0000101295"
    assert filing.company_name == "UNITED GUARDIAN INC"
    assert filing.form == "8-K"
    assert filing.filing_date == date(2026, 5, 15)
    assert filing.primary_document == "f8k_051426.htm"
    assert filing.primary_document_url == (
        "https://www.sec.gov/Archives/edgar/data/101295/000117184326003455/f8k_051426.htm"
    )


def test_resolve_filing_accepts_iso_datetime_filed_at_from_atom_feed() -> None:
    """The Atom feed path passes the full ISO 8601 timestamp; the resolver
    must extract the date portion without choking on the time-and-offset
    suffix."""
    index_url = (
        "https://www.sec.gov/Archives/edgar/data/101295/000117184326003455/"
        "0001171843-26-003455-index.html"
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.get(index_url).mock(return_value=httpx.Response(200, text=_SAMPLE_FILING_INDEX_HTML))
        with EdgarClient(user_agent="filings-watcher tester@example.com") as client:
            filing = resolve_filing(
                cik="0000101295",
                accession_number="0001171843-26-003455",
                company_name="UNITED GUARDIAN INC",
                form="8-K",
                filed_at="2026-05-15T09:05:09-04:00",
                client=client,
            )
    assert filing.filing_date == date(2026, 5, 15)


def test_resolve_filing_attaches_ex99_exhibit_refs() -> None:
    """The sample index has one EX-99.1; the resolver attaches it as a fetch
    target (text empty) with the document-direct URL."""
    index_url = (
        "https://www.sec.gov/Archives/edgar/data/101295/000117184326003455/"
        "0001171843-26-003455-index.html"
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.get(index_url).mock(return_value=httpx.Response(200, text=_SAMPLE_FILING_INDEX_HTML))
        with EdgarClient(user_agent="filings-watcher tester@example.com") as client:
            filing = resolve_filing(
                cik="0000101295",
                accession_number="0001171843-26-003455",
                company_name="UNITED GUARDIAN INC",
                form="8-K",
                filed_at="20260515",
                client=client,
            )
    assert [e.exhibit_type for e in filing.exhibits] == ["EX-99.1"]
    ex = filing.exhibits[0]
    assert ex.document == "ex_99_1.htm"
    assert ex.url == (
        "https://www.sec.gov/Archives/edgar/data/101295/000117184326003455/ex_99_1.htm"
    )
    assert ex.text == ""  # a fetch target; body filled at document fetch


def test_extract_exhibit_99_refs_orders_by_subnumber_and_ignores_non_ex99() -> None:
    refs = _extract_exhibit_99_refs(_MULTI_EXHIBIT_INDEX_HTML, "5", "000000000000000005")
    # 99.1 leads despite appearing after 99.2 in the table; EX-10.1 is excluded.
    assert [e.exhibit_type for e in refs] == ["EX-99.1", "EX-99.2"]
    assert [e.document for e in refs] == ["ex991.htm", "ex992.htm"]


def test_extract_exhibit_99_refs_empty_when_no_exhibits() -> None:
    html = '<table summary="Document Format Files"><tr><td>1</td><td>x</td>'
    html += '<td><a href="/a/f.htm">f.htm</a></td><td>8-K</td><td>1</td></tr></table>'
    assert _extract_exhibit_99_refs(html, "5", "000000000000000005") == []


def test_to_date_accepts_compact_iso_and_iso_datetime() -> None:
    assert _to_date("20260515") == date(2026, 5, 15)
    assert _to_date("2026-05-15") == date(2026, 5, 15)
    assert _to_date("2026-05-15T09:05:09-04:00") == date(2026, 5, 15)


def test_to_date_rejects_unrecognised_format() -> None:
    with pytest.raises(ValueError, match="unrecognised filed_at format"):
        _to_date("May 15, 2026")
