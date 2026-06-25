"""End-to-end test for the scan-form4 CLI (Form 4 insider ingest).

EDGAR HTTP is intercepted by respx; the DB is a tmp SQLite with migrations applied.
No LLM is involved (deterministic XML parse), so no Anthropic config is needed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import text

from filings_orchestrator.cli.scan_form4 import main
from filings_orchestrator.persistence import apply_migrations, open_engine

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()
FIXTURES = Path(__file__).parent / "fixtures"
_OWNERSHIP = (FIXTURES / "form4_ownership.xml").read_text()

_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR2/master.20260624.idx"
_SUB_URL = "https://www.sec.gov/Archives/edgar/data/123/0001234567-26-000001.txt"
_ACCESSION = "0001234567-26-000001"

_MASTER_IDX = (
    "Description:           Master Index of EDGAR Dissemination Feed\n"
    "Last Data Received:    June 24, 2026\n"
    "Comments:              webmaster@sec.gov\n"
    "\n"
    "CIK|Company Name|Form Type|Date Filed|File Name\n"
    "--------------------------------------------------------------------------------\n"
    "123|ACME CORP|4|2026-06-24|edgar/data/123/0001234567-26-000001.txt\n"
    "999|OTHER CO INC|8-K|2026-06-24|edgar/data/999/0009999999-26-000002.txt\n"
)


@pytest.fixture
def configured_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "filings.db"
    monkeypatch.setenv("EDGAR_USER_AGENT", "filings-watcher tester@example.com")
    monkeypatch.setenv("FILINGS_DB_PATH", str(db_path))
    engine = open_engine(str(db_path))
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    return db_path


def _read_jsonl(captured: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in captured.splitlines() if line.strip()]


def test_scan_form4_ingests_insider_transactions(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["scan-form4", "--date", "2026-06-24"])

    with respx.mock(assert_all_called=True) as mock:
        # Only the form-4 entry's submission is fetched; the 8-K row is filtered out.
        mock.get(_INDEX_URL).mock(return_value=httpx.Response(200, text=_MASTER_IDX))
        mock.get(_SUB_URL).mock(return_value=httpx.Response(200, text=_OWNERSHIP))
        main()

    events = _read_jsonl(capsys.readouterr().out)
    names = [e["event"] for e in events]
    assert "tick_started" in names
    assert "form4_index_polled" in names
    assert "tick_completed" in names
    assert "tick_failed" not in names

    polled = next(e for e in events if e["event"] == "form4_index_polled")
    assert polled["entries_total"] == 1  # only the form-4 entry survives filter_form
    assert polled["entries_new"] == 1

    completed = next(e for e in events if e["event"] == "tick_completed")
    assert completed["filings_count"] == 1
    assert completed["transactions_count"] == 2
    assert completed["errors_count"] == 0

    engine = open_engine(str(configured_env))
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT txn_seq, transaction_code, acquired_disposed, shares, "
                "price_per_share, transaction_value, issuer_ticker, is_10b5_1 "
                "FROM insider_transactions WHERE accession_number = :a ORDER BY txn_seq"
            ),
            {"a": _ACCESSION},
        ).fetchall()
    assert len(rows) == 2
    buy = rows[0]
    assert buy[1] == "P" and buy[2] == "A"
    assert buy[3] == 1000.0 and buy[4] == 10.0 and buy[5] == 10000.0
    assert buy[6] == "ACME"
    assert buy[7] == 1  # is_10b5_1 captured
    sell = rows[1]
    assert sell[1] == "S" and sell[2] == "D"


def test_scan_form4_dedups_already_ingested(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A re-run over the same index re-fetches the index but skips the
    already-ingested filing (no submission fetch)."""
    monkeypatch.setattr(sys, "argv", ["scan-form4", "--date", "2026-06-24"])
    with respx.mock(assert_all_called=True) as mock:
        mock.get(_INDEX_URL).mock(return_value=httpx.Response(200, text=_MASTER_IDX))
        mock.get(_SUB_URL).mock(return_value=httpx.Response(200, text=_OWNERSHIP))
        main()
    capsys.readouterr()

    with respx.mock(assert_all_called=True) as mock:
        # No submission mock: the filing is already seen, so it must not be fetched.
        mock.get(_INDEX_URL).mock(return_value=httpx.Response(200, text=_MASTER_IDX))
        main()

    events = _read_jsonl(capsys.readouterr().out)
    polled = next(e for e in events if e["event"] == "form4_index_polled")
    assert polled["entries_new"] == 0
    completed = next(e for e in events if e["event"] == "tick_completed")
    assert completed["filings_count"] == 0
