"""Tests for the shared filing resolver used by both ingest paths."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from filings_orchestrator.edgar import EdgarClient
from filings_orchestrator.edgar.filing_resolver import (
    _extract_primary_document_name,
    _to_date,
    resolve_filing,
)

FIXTURES = Path(__file__).parent / "fixtures"
_SAMPLE_FILING_INDEX_HTML = (FIXTURES / "filing_index_8k.html").read_text()


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


def test_to_date_accepts_compact_iso_and_iso_datetime() -> None:
    assert _to_date("20260515") == date(2026, 5, 15)
    assert _to_date("2026-05-15") == date(2026, 5, 15)
    assert _to_date("2026-05-15T09:05:09-04:00") == date(2026, 5, 15)


def test_to_date_rejects_unrecognised_format() -> None:
    with pytest.raises(ValueError, match="unrecognised filed_at format"):
        _to_date("May 15, 2026")
