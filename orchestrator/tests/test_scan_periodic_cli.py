"""End-to-end tests for the scan-periodic CLI (10-K risk-factor ingest, ADR 0042).

EDGAR HTTP is intercepted by respx; the DB is a tmp SQLite with migrations applied.
Deterministic (segment + store, no LLM), so no Anthropic config is needed.
"""

from __future__ import annotations

import datetime as _dt
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from sqlalchemy import text

from filings_orchestrator.cli.scan_periodic import main
from filings_orchestrator.edgar.filing_resolver import _extract_report_date
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import read_periodic_cursor

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()
FIXTURES = Path(__file__).parent / "fixtures"
_RISK_FACTORS_HTML = (FIXTURES / "sample_10k_risk_factors.html").read_text()
_FILING_INDEX_HTML = (FIXTURES / "filing_index_10k.html").read_text()

_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR1/master.20260315.idx"
_FILING_INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/data/123/000123456726000010/0001234567-26-000010-index.html"
)
_PRIMARY_URL = "https://www.sec.gov/Archives/edgar/data/123/000123456726000010/acme-10k.htm"
_ACCESSION = "0001234567-26-000010"

_FIXED_ET = _dt.datetime(2026, 3, 15, 20, 0, tzinfo=ZoneInfo("America/New_York"))


class _FrozenDatetime(_dt.datetime):
    @classmethod
    def now(cls, tz: _dt.tzinfo | None = None) -> _dt.datetime:  # type: ignore[override]
        return _FIXED_ET.astimezone(tz) if tz is not None else _FIXED_ET.replace(tzinfo=None)


def _master_idx(*rows: str) -> str:
    return (
        "Description:           Master Index of EDGAR Dissemination Feed\n"
        "CIK|Company Name|Form Type|Date Filed|File Name\n"
        "--------------------------------------------------------------------------------\n"
        + "".join(r if r.endswith("\n") else r + "\n" for r in rows)
    )


_MASTER_IDX = _master_idx(
    "123|ACME CORP|10-K|2026-03-15|edgar/data/123/0001234567-26-000010.txt",
    "999|OTHER CO INC|8-K|2026-03-15|edgar/data/999/0009999999-26-000002.txt",
)


@pytest.fixture
def configured_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "filings.db"
    monkeypatch.setenv("EDGAR_USER_AGENT", "filings-watcher tester@example.com")
    monkeypatch.setenv("FILINGS_DB_PATH", str(db_path))
    engine = open_engine(str(db_path))
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    return db_path


def _all_routes(mock: respx.MockRouter) -> None:
    mock.get(_INDEX_URL).mock(return_value=httpx.Response(200, text=_MASTER_IDX))
    mock.get(_FILING_INDEX_URL).mock(return_value=httpx.Response(200, text=_FILING_INDEX_HTML))
    mock.get(_PRIMARY_URL).mock(return_value=httpx.Response(200, text=_RISK_FACTORS_HTML))


def _envelope(db_path: Path) -> dict[str, object]:
    engine = open_engine(str(db_path))
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT cik, form, period_of_report, fiscal_year, parsed, block_count "
                "FROM periodic_filings WHERE accession_number = :a"
            ),
            {"a": _ACCESSION},
        ).one()
    return {
        "cik": row[0],
        "form": row[1],
        "period_of_report": row[2],
        "fiscal_year": row[3],
        "parsed": row[4],
        "block_count": row[5],
    }


def _block_texts(db_path: Path) -> list[str]:
    engine = open_engine(str(db_path))
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT block_text FROM filing_blocks WHERE accession_number = :a "
                "ORDER BY block_index"
            ),
            {"a": _ACCESSION},
        ).fetchall()
    return [r[0] for r in rows]


def test_ingests_10k_blocks_by_date(configured_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["scan-periodic", "--date", "2026-03-15"])
    with respx.mock(assert_all_called=True) as mock:
        _all_routes(mock)
        main()

    assert _envelope(configured_env) == {
        "cik": "0000000123",
        "form": "10-K",
        "period_of_report": "2025-12-31",  # resolved from the filing-index page
        "fiscal_year": 2025,
        "parsed": 1,
        "block_count": 4,  # intro + three risk factors
    }
    assert any("going concern" in t.lower() for t in _block_texts(configured_env))
    # --date is a manual override: it must not touch the cursor.
    assert read_periodic_cursor(open_engine(str(configured_env))) is None


def test_non_10k_rows_are_not_ingested(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["scan-periodic", "--date", "2026-03-15"])
    with respx.mock(assert_all_called=True) as mock:
        _all_routes(mock)
        main()
    engine = open_engine(str(configured_env))
    with engine.begin() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM periodic_filings")).scalar()
    assert count == 1  # the 8-K row was filtered out


def test_cursor_driven_advances_then_dedups(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("filings_orchestrator.cli.scan_periodic.datetime", _FrozenDatetime)
    monkeypatch.setattr(sys, "argv", ["scan-periodic"])  # cursor-driven

    with respx.mock(assert_all_called=True) as mock:
        _all_routes(mock)
        main()
    assert read_periodic_cursor(open_engine(str(configured_env))) == (_ACCESSION, "2026-03-15")

    # Second cursor-driven run: the filing is anchored, so only the index is fetched
    # (no filing-index / primary re-fetch). assert_all_called with just the index
    # route would fail if a re-fetch were attempted (it would hit an unmocked URL).
    with respx.mock(assert_all_called=True) as mock:
        mock.get(_INDEX_URL).mock(return_value=httpx.Response(200, text=_MASTER_IDX))
        main()


def test_extract_report_date_from_index_page() -> None:
    assert _extract_report_date(_FILING_INDEX_HTML) == date(2025, 12, 31)


def test_extract_report_date_absent_returns_none() -> None:
    html = "<html><body><table summary='Document Format Files'></table></body></html>"
    assert _extract_report_date(html) is None
