"""Tests for the EDGAR client and parsing logic.

Live HTTP calls are intercepted by respx — no network in CI.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from filings_orchestrator.edgar import EdgarClient, recent_8k_filings, ticker_to_cik
from filings_orchestrator.edgar.client import RateLimiter

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text())


def test_client_requires_email_in_user_agent() -> None:
    with pytest.raises(ValueError, match="contact email"):
        EdgarClient(user_agent="no-email-here")


def test_ticker_to_cik_pads_to_ten_digits() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://www.sec.gov/files/company_tickers.json").mock(
            return_value=httpx.Response(200, json=_fixture("ticker_index.json"))
        )
        with EdgarClient(user_agent="filings-watcher tester@example.com") as client:
            cik, name = ticker_to_cik("AAPL", client)
    assert cik == "0000320193"
    assert name == "Apple Inc."


def test_ticker_to_cik_is_case_insensitive() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://www.sec.gov/files/company_tickers.json").mock(
            return_value=httpx.Response(200, json=_fixture("ticker_index.json"))
        )
        with EdgarClient(user_agent="filings-watcher tester@example.com") as client:
            cik, _ = ticker_to_cik("aapl", client)
    assert cik == "0000320193"


def test_ticker_to_cik_raises_on_missing() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://www.sec.gov/files/company_tickers.json").mock(
            return_value=httpx.Response(200, json=_fixture("ticker_index.json"))
        )
        with EdgarClient(user_agent="filings-watcher tester@example.com") as client:
            with pytest.raises(LookupError, match="NOTREAL"):
                ticker_to_cik("NOTREAL", client)


def test_recent_8k_filings_filters_form_and_parses_items() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://www.sec.gov/files/company_tickers.json").mock(
            return_value=httpx.Response(200, json=_fixture("ticker_index.json"))
        )
        mock.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
            return_value=httpx.Response(200, json=_fixture("submissions_aapl.json"))
        )
        with EdgarClient(user_agent="filings-watcher tester@example.com") as client:
            filings = recent_8k_filings("AAPL", client)

    # Only 8-Ks should be returned; the 10-Q in the fixture is excluded.
    assert len(filings) == 2
    assert all(f.form == "8-K" for f in filings)

    # Newest first, items parsed.
    assert filings[0].filing_date == date(2026, 4, 30)
    assert [item.number for item in filings[0].items] == ["2.02", "9.01"]

    # Empty items field parses to empty list, not [""].
    assert filings[1].items == []

    # Optional report_date handled when empty.
    assert filings[1].report_date is None

    # URL is constructed correctly (unpadded CIK, accession dashes stripped).
    expected_url = (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000045/aapl-20260430.htm"
    )
    assert filings[0].primary_document_url == expected_url


def test_recent_8k_filings_respects_limit() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://www.sec.gov/files/company_tickers.json").mock(
            return_value=httpx.Response(200, json=_fixture("ticker_index.json"))
        )
        mock.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
            return_value=httpx.Response(200, json=_fixture("submissions_aapl.json"))
        )
        with EdgarClient(user_agent="filings-watcher tester@example.com") as client:
            filings = recent_8k_filings("AAPL", client, limit=1)
    assert len(filings) == 1


def test_rate_limiter_admits_under_quota_without_blocking() -> None:
    """A burst at or below the per-second quota must not sleep."""
    import time

    limiter = RateLimiter(max_per_second=5)
    start = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.1


def test_rate_limiter_blocks_when_quota_exceeded() -> None:
    """The 6th request in a 1-second window must wait."""
    import time

    limiter = RateLimiter(max_per_second=5)
    for _ in range(5):
        limiter.acquire()
    start = time.monotonic()
    limiter.acquire()
    elapsed = time.monotonic() - start
    # Should have slept close to a full second (allow some scheduling slack).
    assert 0.8 < elapsed < 1.5
