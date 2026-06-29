"""End-to-end tests for the scan-form4 CLI (cursor-driven Form 4 insider ingest).

EDGAR HTTP is intercepted by respx; the DB is a tmp SQLite with migrations applied.
No LLM is involved (deterministic XML parse), so no Anthropic config is needed.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from sqlalchemy import text

from filings_orchestrator.cli.scan_form4 import main
from filings_orchestrator.persistence import apply_migrations, open_engine

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()
FIXTURES = Path(__file__).parent / "fixtures"
_OWNERSHIP = (FIXTURES / "form4_ownership.xml").read_text()
_OPTIONONLY = (FIXTURES / "form4_optiononly.xml").read_text()

_INDEX_URL_0624 = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR2/master.20260624.idx"
_INDEX_URL_0625 = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR2/master.20260625.idx"
_INDEX_URL_0626 = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR2/master.20260626.idx"
_SUB_URL = "https://www.sec.gov/Archives/edgar/data/123/0001234567-26-000001.txt"
_ACCESSION = "0001234567-26-000001"

# Frozen "now" so cursor-driven scans have a deterministic today (ET = 2026-06-26).
_FIXED_ET = _dt.datetime(2026, 6, 26, 20, 0, tzinfo=ZoneInfo("America/New_York"))


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


_MASTER_IDX_0624 = _master_idx(
    "123|ACME CORP|4|2026-06-24|edgar/data/123/0001234567-26-000001.txt",
    "999|OTHER CO INC|8-K|2026-06-24|edgar/data/999/0009999999-26-000002.txt",
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


def _event(events: list[dict[str, object]], name: str) -> dict[str, object]:
    return next(e for e in events if e["event"] == name)


def test_scan_form4_ingests_insider_transactions(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--date manual mode: ingest a single date's Form 4s into both tables."""
    monkeypatch.setattr(sys, "argv", ["scan-form4", "--date", "2026-06-24"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get(_INDEX_URL_0624).mock(return_value=httpx.Response(200, text=_MASTER_IDX_0624))
        mock.get(_SUB_URL).mock(return_value=httpx.Response(200, text=_OWNERSHIP))
        main()

    events = _read_jsonl(capsys.readouterr().out)
    assert "tick_failed" not in [e["event"] for e in events]
    polled = _event(events, "form4_index_polled")
    assert polled["entries_total"] == 1  # only the form-4 entry survives filter_form
    assert polled["entries_new"] == 1
    completed = _event(events, "tick_completed")
    assert completed["filings_count"] == 1
    assert completed["transactions_count"] == 2
    assert completed["errors_count"] == 0

    engine = open_engine(str(configured_env))
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT transaction_code, acquired_disposed, shares, price_per_share, "
                "transaction_value, issuer_ticker, is_10b5_1 "
                "FROM insider_transactions WHERE accession_number = :a ORDER BY txn_seq"
            ),
            {"a": _ACCESSION},
        ).fetchall()
        env = conn.execute(
            text(
                "SELECT parsed, non_derivative_count, issuer_ticker FROM insider_filings "
                "WHERE accession_number = :a"
            ),
            {"a": _ACCESSION},
        ).fetchone()
    assert len(rows) == 2
    assert rows[0][0] == "P" and rows[0][4] == 10000.0 and rows[0][5] == "ACME" and rows[0][6] == 1
    assert rows[1][0] == "S"
    # The envelope anchors the filing.
    assert env is not None and env[0] == 1 and env[1] == 2 and env[2] == "ACME"


def test_scan_form4_dedups_via_envelope(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A re-run skips an already-anchored filing — no submission re-fetch."""
    monkeypatch.setattr(sys, "argv", ["scan-form4", "--date", "2026-06-24"])
    with respx.mock(assert_all_called=True) as mock:
        mock.get(_INDEX_URL_0624).mock(return_value=httpx.Response(200, text=_MASTER_IDX_0624))
        mock.get(_SUB_URL).mock(return_value=httpx.Response(200, text=_OWNERSHIP))
        main()
    capsys.readouterr()

    with respx.mock(assert_all_called=True) as mock:
        # No submission mock: the filing is anchored in insider_filings, so it must
        # not be fetched again.
        mock.get(_INDEX_URL_0624).mock(return_value=httpx.Response(200, text=_MASTER_IDX_0624))
        main()

    events = _read_jsonl(capsys.readouterr().out)
    assert _event(events, "form4_index_polled")["entries_new"] == 0
    assert _event(events, "tick_completed")["filings_count"] == 0


def test_scan_form4_option_only_filing_is_anchored(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An option-only Form 4 yields zero non-derivative rows but is still anchored
    in insider_filings — so it dedups and is never re-fetched (the gap fix)."""
    sub_url = "https://www.sec.gov/Archives/edgar/data/789/0007890000-26-000003.txt"
    idx = _master_idx("789|OPTION CO|4|2026-06-26|edgar/data/789/0007890000-26-000003.txt")
    monkeypatch.setattr(sys, "argv", ["scan-form4", "--date", "2026-06-26"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get(_INDEX_URL_0626).mock(return_value=httpx.Response(200, text=idx))
        mock.get(sub_url).mock(return_value=httpx.Response(200, text=_OPTIONONLY))
        main()

    completed = _event(_read_jsonl(capsys.readouterr().out), "tick_completed")
    assert completed["transactions_count"] == 0
    assert completed["derivative_transactions_count"] == 1

    engine = open_engine(str(configured_env))
    with engine.begin() as conn:
        txns = conn.execute(text("SELECT COUNT(*) FROM insider_transactions")).scalar()
        deriv = conn.execute(
            text(
                "SELECT transaction_code, conversion_exercise_price, expiration_date, "
                "underlying_shares FROM insider_derivative_transactions"
            )
        ).fetchone()
        env = conn.execute(
            text("SELECT parsed, non_derivative_count, derivative_count FROM insider_filings")
        ).fetchone()
    assert txns == 0  # no non-derivative transactions stored
    assert deriv is not None and deriv[0] == "A" and deriv[1] == 15.5
    assert deriv[2] == "2036-06-26" and deriv[3] == 5000.0
    assert env is not None and env[0] == 1 and env[1] == 0 and env[2] == 1  # anchored, 1 deriv

    # Re-run: anchored → no submission fetch.
    with respx.mock(assert_all_called=True) as mock:
        mock.get(_INDEX_URL_0626).mock(return_value=httpx.Response(200, text=idx))
        main()
    events = _read_jsonl(capsys.readouterr().out)
    assert _event(events, "form4_index_polled")["entries_new"] == 0


def test_scan_form4_cursor_first_tick_scans_today_and_advances(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cursor-driven first tick scans only today (ET) and advances the cursor."""
    monkeypatch.setattr("filings_orchestrator.cli.scan_form4.datetime", _FrozenDatetime)
    monkeypatch.setattr(sys, "argv", ["scan-form4"])  # no --date → cursor-driven
    sub_url = "https://www.sec.gov/Archives/edgar/data/123/0001234567-26-000001.txt"
    idx = _master_idx("123|ACME CORP|4|2026-06-26|edgar/data/123/0001234567-26-000001.txt")

    with respx.mock(assert_all_called=True) as mock:
        mock.get(_INDEX_URL_0626).mock(return_value=httpx.Response(200, text=idx))
        mock.get(sub_url).mock(return_value=httpx.Response(200, text=_OWNERSHIP))
        main()

    events = _read_jsonl(capsys.readouterr().out)
    assert _event(events, "tick_started")["dates_to_scan"] == 1
    assert _event(events, "tick_started")["backfill"] is False
    assert _event(events, "cursor_advanced")["filed_at"] == "2026-06-26"
    engine = open_engine(str(configured_env))
    with engine.begin() as conn:
        cursor = conn.execute(
            text("SELECT last_accession_number, last_filed_at FROM form4_ingest_cursor")
        ).fetchone()
    assert cursor is not None and cursor[0] == _ACCESSION and cursor[1] == "2026-06-26"


def test_scan_form4_resumes_after_budget_defer(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A tick capped mid-date does NOT advance the cursor; the next run resumes
    and fills the gap. This is the resume-after-abort contract."""
    monkeypatch.setattr("filings_orchestrator.cli.scan_form4.datetime", _FrozenDatetime)
    monkeypatch.setattr(sys, "argv", ["scan-form4"])
    monkeypatch.setenv("MAX_FORM4_PER_TICK", "1")
    acc1, acc2 = "0001111111-26-000001", "0001111111-26-000002"
    sub1 = f"https://www.sec.gov/Archives/edgar/data/111/{acc1}.txt"
    sub2 = f"https://www.sec.gov/Archives/edgar/data/111/{acc2}.txt"
    idx = _master_idx(
        f"111|TWO FILER A|4|2026-06-26|edgar/data/111/{acc1}.txt",
        f"111|TWO FILER B|4|2026-06-26|edgar/data/111/{acc2}.txt",
    )
    engine = open_engine(str(configured_env))

    # First run: budget 1 → processes acc1, defers acc2, cursor NOT advanced.
    with respx.mock(assert_all_called=True) as mock:
        mock.get(_INDEX_URL_0626).mock(return_value=httpx.Response(200, text=idx))
        mock.get(sub1).mock(return_value=httpx.Response(200, text=_OWNERSHIP))
        main()
    events = _read_jsonl(capsys.readouterr().out)
    assert _event(events, "form4_index_polled")["entries_deferred"] == 1
    assert "cursor_advanced" not in [e["event"] for e in events]
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM form4_ingest_cursor")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM insider_filings")).scalar() == 1

    # Second run: re-scans today, dedups acc1, processes acc2 → complete → advances.
    with respx.mock(assert_all_called=True) as mock:
        mock.get(_INDEX_URL_0626).mock(return_value=httpx.Response(200, text=idx))
        mock.get(sub2).mock(return_value=httpx.Response(200, text=_OWNERSHIP))
        main()
    events = _read_jsonl(capsys.readouterr().out)
    assert _event(events, "cursor_advanced")["filed_at"] == "2026-06-26"
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM insider_filings")).scalar() == 2
        assert conn.execute(text("SELECT COUNT(*) FROM form4_ingest_cursor")).scalar() == 1


def test_scan_form4_backfill_range_newest_first_no_cursor(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--since/--until ingests the whole range newest-first, uncapped, without
    touching the cursor — the backfill driver."""
    acc_a, acc_b = "0002000000-26-000025", "0002000000-26-000026"
    sub_a = f"https://www.sec.gov/Archives/edgar/data/200/{acc_a}.txt"
    sub_b = f"https://www.sec.gov/Archives/edgar/data/200/{acc_b}.txt"
    idx25 = _master_idx(f"200|BACKFILL CO|4|2026-06-25|edgar/data/200/{acc_a}.txt")
    idx26 = _master_idx(f"200|BACKFILL CO|4|2026-06-26|edgar/data/200/{acc_b}.txt")
    monkeypatch.setattr(
        sys, "argv", ["scan-form4", "--since", "2026-06-25", "--until", "2026-06-26", "--rate", "5"]
    )

    with respx.mock(assert_all_called=True) as mock:
        mock.get(_INDEX_URL_0625).mock(return_value=httpx.Response(200, text=idx25))
        mock.get(_INDEX_URL_0626).mock(return_value=httpx.Response(200, text=idx26))
        mock.get(sub_a).mock(return_value=httpx.Response(200, text=_OWNERSHIP))
        mock.get(sub_b).mock(return_value=httpx.Response(200, text=_OWNERSHIP))
        main()

    events = _read_jsonl(capsys.readouterr().out)
    started = _event(events, "tick_started")
    assert started["backfill"] is True
    assert started["rate_per_sec"] == 5
    assert started["dates_to_scan"] == 2
    # Newest-first: 06-26 is polled before 06-25.
    polled = [e["index_date"] for e in events if e["event"] == "form4_index_polled"]
    assert polled == ["2026-06-26", "2026-06-25"]
    assert _event(events, "tick_completed")["filings_count"] == 2

    engine = open_engine(str(configured_env))
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM insider_filings")).scalar() == 2
        # Backfill never touches the cursor.
        assert conn.execute(text("SELECT COUNT(*) FROM form4_ingest_cursor")).scalar() == 0


def test_scan_form4_rejects_date_with_since(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["scan-form4", "--date", "2026-06-26", "--since", "2026-06-01"]
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
