"""Tests for the cik_tickers repository functions.

Migrations are applied against a tmp_path SQLite file; tests exercise the
upsert/lookup/backfill paths directly without network. The scan-tickers
CLI's HTTP fetch is covered separately by mocking httpx.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    backfill_filings_tickers,
    lookup_ticker_by_cik,
    upsert_cik_tickers,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()


@pytest.fixture
def engine(tmp_path: Path):
    db = tmp_path / "filings.db"
    e = open_engine(str(db))
    apply_migrations(e, migrations_dir=MIGRATIONS_DIR)
    return e


def test_upsert_inserts_new_rows(engine):
    written = upsert_cik_tickers(
        engine,
        [
            ("0000320193", "AAPL", "Apple Inc."),
            ("0000789019", "MSFT", "MICROSOFT CORP"),
        ],
    )
    assert written == 2
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT cik, ticker, company_name FROM cik_tickers ORDER BY cik")
        ).fetchall()
    assert rows == [
        ("0000320193", "AAPL", "Apple Inc."),
        ("0000789019", "MSFT", "MICROSOFT CORP"),
    ]


def test_upsert_overwrites_on_conflict(engine):
    """The mapping is current-state; ticker changes overwrite the row."""
    upsert_cik_tickers(engine, [("0001326801", "FB", "Facebook, Inc.")])
    upsert_cik_tickers(engine, [("0001326801", "META", "Meta Platforms, Inc.")])
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT ticker, company_name FROM cik_tickers WHERE cik = '0001326801'")
        ).fetchone()
    assert row == ("META", "Meta Platforms, Inc.")


def test_upsert_empty_input_is_noop(engine):
    assert upsert_cik_tickers(engine, []) == 0


def test_lookup_returns_ticker_when_present(engine):
    upsert_cik_tickers(engine, [("0000320193", "AAPL", "Apple Inc.")])
    assert lookup_ticker_by_cik(engine, "0000320193") == "AAPL"


def test_lookup_returns_none_when_missing(engine):
    # No upsert; the table is empty.
    assert lookup_ticker_by_cik(engine, "0000000000") is None


def test_backfill_updates_only_null_ticker_rows(engine):
    # Pre-populate cik_tickers
    upsert_cik_tickers(
        engine,
        [
            ("0000320193", "AAPL", "Apple Inc."),
            ("0000789019", "MSFT", "MICROSOFT CORP"),
        ],
    )
    # Insert two filings: one with no ticker (will be backfilled),
    # one with a pre-existing ticker (must be left alone).
    _insert_filing(engine, "aaa-26-001", "0000320193", None, "Apple Inc.")
    _insert_filing(engine, "bbb-26-001", "0000789019", "CUSTOM", "MICROSOFT CORP")
    _insert_filing(engine, "ccc-26-001", "9999999999", None, "Unknown Co")

    updated = backfill_filings_tickers(engine)
    # One row backfilled: aaa-26-001. The pre-existing CUSTOM is left alone.
    # The Unknown Co row has no cik_tickers match, so it stays NULL.
    assert updated == 1

    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT accession_number, ticker FROM filings ORDER BY accession_number")
        ).fetchall()
    assert rows == [
        ("aaa-26-001", "AAPL"),
        ("bbb-26-001", "CUSTOM"),
        ("ccc-26-001", None),
    ]


def test_backfill_is_idempotent(engine):
    upsert_cik_tickers(engine, [("0000320193", "AAPL", "Apple Inc.")])
    _insert_filing(engine, "aaa-26-001", "0000320193", None, "Apple Inc.")
    first = backfill_filings_tickers(engine)
    second = backfill_filings_tickers(engine)
    assert first == 1
    assert second == 0  # no NULL rows remain, so the second pass is a no-op


def _insert_filing(engine, accession: str, cik: str, ticker: str | None, company: str) -> None:
    """Minimal filings-row insert for tests; only the columns we exercise."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO filings (
                    accession_number, cik, ticker, company_name, form,
                    filing_date, primary_document, primary_document_url,
                    items_json, fetched_at
                ) VALUES (
                    :accession, :cik, :ticker, :company, '8-K',
                    '2026-05-20', 'doc.htm', 'http://example/doc.htm',
                    '[]', '2026-05-20T00:00:00Z'
                )
                """
            ),
            {
                "accession": accession,
                "cik": cik,
                "ticker": ticker,
                "company": company,
            },
        )
