"""End-to-end tests for the scan-tickers CLI.

The HTTP fetch from SEC is intercepted via respx; the DB is a tmp_path
SQLite file with migrations applied.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import text

from filings_orchestrator.cli.scan_tickers import _normalize_payload, main
from filings_orchestrator.persistence import apply_migrations, open_engine

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()


@pytest.fixture
def configured_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "filings.db"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-langsmith-key")
    monkeypatch.setenv("LANGSMITH_PROJECT", "filings-watcher-test")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("EDGAR_USER_AGENT", "filings-watcher tester@example.com")
    monkeypatch.setenv("FILINGS_DB_PATH", str(db_path))

    engine = open_engine(str(db_path))
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    return db_path


def test_normalize_payload_zero_pads_cik() -> None:
    payload = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 1, "ticker": "TEST", "title": "Single Digit CIK"},
    }
    out = _normalize_payload(payload)
    assert ("0000320193", "AAPL", "Apple Inc.") in out
    assert ("0000000001", "TEST", "Single Digit CIK") in out


def test_normalize_payload_skips_entries_missing_required_fields() -> None:
    payload = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        # Missing ticker
        "1": {"cik_str": 12345, "title": "Some Co"},
        # Missing title
        "2": {"cik_str": 67890, "ticker": "BAR"},
        # Missing cik_str
        "3": {"ticker": "BAZ", "title": "Baz Inc."},
        # Not a dict
        "4": "garbage",
    }
    out = _normalize_payload(payload)
    assert out == [("0000320193", "AAPL", "Apple Inc.")]


def test_scan_tickers_main_writes_table_and_backfills_filings(
    configured_env: Path,
) -> None:
    # Pre-existing filing with NULL ticker; backfill should populate it.
    engine = open_engine(str(configured_env))
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO filings
                       (accession_number, cik, ticker, company_name, form,
                        filing_date, primary_document, primary_document_url,
                        items_json, fetched_at)
                VALUES
                       ('test-26-001', '0000320193', NULL, 'Apple Inc.', '8-K',
                        '2026-05-20', 'a.htm', 'http://example/a.htm', '[]',
                        '2026-05-20T00:00:00Z')
                """
            )
        )

    sec_payload = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    }
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://www.sec.gov/files/company_tickers.json").mock(
            return_value=httpx.Response(200, text=json.dumps(sec_payload))
        )
        main()

    with engine.begin() as conn:
        ticker_count = conn.execute(text("SELECT COUNT(*) FROM cik_tickers")).scalar()
        backfilled_ticker = conn.execute(
            text("SELECT ticker FROM filings WHERE accession_number = 'test-26-001'")
        ).scalar()

    assert ticker_count == 2
    assert backfilled_ticker == "AAPL"
