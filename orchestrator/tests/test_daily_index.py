"""Tests for the EDGAR daily-index ingest path."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import respx

from filings_orchestrator.edgar import EdgarClient
from filings_orchestrator.edgar.daily_index import (
    daily_index_url,
    fetch_daily_index,
    filter_form,
    parse_daily_index,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


_SAMPLE_MASTER_IDX = _fixture_text("master_20260515.idx")


def test_daily_index_url_computes_correct_quarter() -> None:
    assert daily_index_url(date(2026, 1, 1)).endswith("/2026/QTR1/master.20260101.idx")
    assert daily_index_url(date(2026, 3, 31)).endswith("/2026/QTR1/master.20260331.idx")
    assert daily_index_url(date(2026, 4, 1)).endswith("/2026/QTR2/master.20260401.idx")
    assert daily_index_url(date(2026, 7, 31)).endswith("/2026/QTR3/master.20260731.idx")
    assert daily_index_url(date(2026, 12, 31)).endswith("/2026/QTR4/master.20261231.idx")


def test_parse_daily_index_drops_preamble_and_malformed_rows() -> None:
    entries = parse_daily_index(_SAMPLE_MASTER_IDX)
    forms = [e.form for e in entries]
    # Preamble lines and the malformed row are dropped; the four well-formed
    # rows survive (including the 13F-HR and the 8-K/A — filter_form is a
    # separate concern).
    assert forms == ["13F-HR", "8-K", "8-K/A", "8-K"]


def test_parse_daily_index_extracts_accession_and_zero_pads_cik() -> None:
    entries = parse_daily_index(_SAMPLE_MASTER_IDX)
    apple = next(e for e in entries if "Apple" in e.company_name)
    assert apple.accession_number == "0000320193-26-000099"
    assert apple.cik == "0000320193"
    assert apple.filed_at == "20260515"


def test_filter_form_is_exact_match() -> None:
    """8-K/A amendments are a separate form and must not be picked up by
    a plain '8-K' filter — handling them is out of slice 6 scope."""
    entries = parse_daily_index(_SAMPLE_MASTER_IDX)
    only_8k = filter_form(entries, "8-K")
    assert {e.accession_number for e in only_8k} == {
        "0001171843-26-003455",
        "0001193125-26-225361",
    }


def test_fetch_daily_index_uses_the_correct_url() -> None:
    target_url = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR2/master.20260515.idx"
    with respx.mock(assert_all_called=True) as mock:
        mock.get(target_url).mock(return_value=httpx.Response(200, text=_SAMPLE_MASTER_IDX))
        with EdgarClient(user_agent="filings-watcher tester@example.com") as client:
            text = fetch_daily_index(date(2026, 5, 15), client)
    assert "8-K" in text
