"""End-to-end tests for the scan-daily-index CLI.

Live HTTP (EDGAR) is intercepted by respx. The Anthropic classifier is
monkeypatched at its module-level entry point so the test is fully
hermetic. The DB is a tmp_path SQLite file with migrations applied.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import text

from filings_orchestrator.classify.schema import (
    Classification,
    FilingClassification,
)
from filings_orchestrator.classify.taxonomy import EventType
from filings_orchestrator.cli.scan_daily_index import _dates_to_scan, main
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import read_ingest_cursor

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()
FIXTURES = Path(__file__).parent / "fixtures"

_MASTER_IDX_BODY = (FIXTURES / "master_cli_minimal.idx").read_text()
_FILING_INDEX_HTML = (FIXTURES / "filing_index_8k.html").read_text()

_FILING_BODY_HTML = """<html><body>
<p>Item 8.01 Other Events.</p>
<p>The Company announces a strategic shift.</p>
</body></html>
"""


@pytest.fixture
def configured_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set the env vars `load_config()` requires and point the DB at tmp."""
    db_path = tmp_path / "filings.db"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-langsmith-key")
    monkeypatch.setenv("LANGSMITH_PROJECT", "filings-watcher-test")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("EDGAR_USER_AGENT", "filings-watcher tester@example.com")
    monkeypatch.setenv("FILINGS_DB_PATH", str(db_path))

    # Pre-apply migrations so the CLI starts against a ready schema.
    engine = open_engine(str(db_path))
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    return db_path


def _stub_classify_filing(document: object) -> FilingClassification:
    """Replacement for the real classifier — no Anthropic calls, deterministic output."""
    from filings_orchestrator.edgar.document import FilingDocument

    assert isinstance(document, FilingDocument)
    return FilingClassification(
        accession_number=document.filing.accession_number,
        cik=document.filing.cik,
        company_name=document.filing.company_name,
        filing_date=document.filing.filing_date.isoformat(),
        items=[],
        whole_filing=Classification(
            event_type=EventType.OTHER_MATERIAL,
            is_material=True,
            confidence=0.9,
            reasoning="stub classifier",
        ),
        classified_at=datetime.now(UTC),
        model="haiku-test",
        classifier_version="haiku-test+prompt-deadbeef",
        taxonomy_version="v1-test",
    )


def _read_jsonl(captured: str) -> list[dict[str, object]]:
    """Parse pytest's captured stdout — one JSON object per non-empty line."""
    return [json.loads(line) for line in captured.splitlines() if line.strip()]


def test_scan_daily_index_classifies_new_8k_and_advances_cursor(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "filings_orchestrator.cli.scan_daily_index.classify_filing",
        _stub_classify_filing,
    )
    # Pin the "today_et" the CLI sees so the test is date-stable.
    fixed_now = datetime(2026, 5, 15, 16, 0, 0, tzinfo=UTC)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return fixed_now if tz is None else fixed_now.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr("filings_orchestrator.cli.scan_daily_index.datetime", _FixedDateTime)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR2/master.20260515.idx"
        ).mock(return_value=httpx.Response(200, text=_MASTER_IDX_BODY))
        mock.get(
            "https://www.sec.gov/Archives/edgar/data/101295/000117184326003455/"
            "0001171843-26-003455-index.html"
        ).mock(return_value=httpx.Response(200, text=_FILING_INDEX_HTML))
        mock.get(
            "https://www.sec.gov/Archives/edgar/data/101295/000117184326003455/f8k_051426.htm"
        ).mock(return_value=httpx.Response(200, text=_FILING_BODY_HTML))

        main()

    out = capsys.readouterr().out
    events = _read_jsonl(out)
    event_names = [e["event"] for e in events]
    assert "tick_started" in event_names
    assert "filing_fetched" in event_names
    assert "classification_started" in event_names
    assert "classification_completed" in event_names
    assert "reduce_completed" in event_names
    assert "cursor_advanced" in event_names
    assert "tick_completed" in event_names
    assert "tick_failed" not in event_names

    # The reduce stage ran inline as its own run and wrote the events layer.
    # The stub yields a whole-filing classification (no Items), so reduce is a
    # pass-through: one event, no model call — keeping the test hermetic.
    reduce_done = next(e for e in events if e["event"] == "reduce_completed")
    assert reduce_done["events"] == 1
    completed = next(e for e in events if e["event"] == "tick_completed")
    assert completed["reduce_errors_count"] == 0

    # The 13F-HR row must not appear — only the 8-K is classified.
    fetched = next(e for e in events if e["event"] == "filing_fetched")
    assert fetched["accession_number"] == "0001171843-26-003455"
    assert fetched["form"] == "8-K"

    # Cursor advanced to the classified filing.
    engine = open_engine(str(configured_env))
    assert read_ingest_cursor(engine) == ("0001171843-26-003455", "20260515")

    # The 13F-HR was not persisted.
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT accession_number, form FROM filings")).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "0001171843-26-003455"

    # An events row exists under a succeeded reduce run for the classified filing.
    with engine.begin() as conn:
        event_rows = conn.execute(
            text("SELECT accession_number FROM events WHERE accession_number = :a"),
            {"a": "0001171843-26-003455"},
        ).fetchall()
        reduce_runs = conn.execute(
            text("SELECT status FROM runs WHERE stage = 'reduce'")
        ).fetchall()
    assert len(event_rows) == 1
    assert [r[0] for r in reduce_runs] == ["succeeded"]


def test_scan_daily_index_reduce_failure_is_non_fatal(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A reduce failure must not fail the tick: the classification is already
    persisted and reduce is a derived, replayable stage (ADR 0028). The tick
    completes, the cursor advances, and the failure is logged + counted."""
    monkeypatch.setattr(
        "filings_orchestrator.cli.scan_daily_index.classify_filing",
        _stub_classify_filing,
    )

    def _boom(_classification: object) -> object:
        raise ValueError("reduce blew up")

    # Patched at the tick's module boundary; ValueError is non-retryable, so
    # with_retries propagates it straight into _reduce_one's handler.
    monkeypatch.setattr("filings_orchestrator.cli.scan_daily_index.reduce_filing", _boom)

    fixed_now = datetime(2026, 5, 15, 16, 0, 0, tzinfo=UTC)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return fixed_now if tz is None else fixed_now.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr("filings_orchestrator.cli.scan_daily_index.datetime", _FixedDateTime)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR2/master.20260515.idx"
        ).mock(return_value=httpx.Response(200, text=_MASTER_IDX_BODY))
        mock.get(
            "https://www.sec.gov/Archives/edgar/data/101295/000117184326003455/"
            "0001171843-26-003455-index.html"
        ).mock(return_value=httpx.Response(200, text=_FILING_INDEX_HTML))
        mock.get(
            "https://www.sec.gov/Archives/edgar/data/101295/000117184326003455/f8k_051426.htm"
        ).mock(return_value=httpx.Response(200, text=_FILING_BODY_HTML))

        main()

    events = _read_jsonl(capsys.readouterr().out)
    event_names = [e["event"] for e in events]
    assert "reduce_failed" in event_names
    assert "tick_failed" not in event_names
    assert "cursor_advanced" in event_names

    completed = next(e for e in events if e["event"] == "tick_completed")
    assert completed["reduce_errors_count"] == 1
    assert completed["new_filings_count"] == 1

    # Cursor advanced despite the reduce failure; the classification persisted;
    # the failed reduce run is recorded; no events row was written.
    engine = open_engine(str(configured_env))
    assert read_ingest_cursor(engine) == ("0001171843-26-003455", "20260515")
    with engine.begin() as conn:
        class_count = conn.execute(text("SELECT COUNT(*) FROM classifications")).scalar()
        event_count = conn.execute(text("SELECT COUNT(*) FROM events")).scalar()
        reduce_runs = conn.execute(
            text("SELECT status FROM runs WHERE stage = 'reduce'")
        ).fetchall()
    assert class_count == 1
    assert event_count == 0
    assert [r[0] for r in reduce_runs] == ["failed"]


def test_scan_daily_index_idempotent_on_already_seen_filing(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Re-running with the same filing already in the DB classifies nothing
    new (dedup via accession-number PK, the cursor is a query narrower)."""
    monkeypatch.setattr(
        "filings_orchestrator.cli.scan_daily_index.classify_filing",
        _stub_classify_filing,
    )
    fixed_now = datetime(2026, 5, 15, 16, 0, 0, tzinfo=UTC)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return fixed_now if tz is None else fixed_now.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr("filings_orchestrator.cli.scan_daily_index.datetime", _FixedDateTime)

    def _run() -> str:
        with respx.mock(assert_all_called=False) as mock:
            mock.get(
                "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR2/master.20260515.idx"
            ).mock(return_value=httpx.Response(200, text=_MASTER_IDX_BODY))
            mock.get(
                "https://www.sec.gov/Archives/edgar/data/101295/000117184326003455/"
                "0001171843-26-003455-index.html"
            ).mock(return_value=httpx.Response(200, text=_FILING_INDEX_HTML))
            mock.get(
                "https://www.sec.gov/Archives/edgar/data/101295/000117184326003455/f8k_051426.htm"
            ).mock(return_value=httpx.Response(200, text=_FILING_BODY_HTML))
            main()
        return capsys.readouterr().out

    _run()
    second_out = _run()
    events = _read_jsonl(second_out)
    completed = next(e for e in events if e["event"] == "tick_completed")
    # No new filings processed on the second pass.
    assert completed["new_filings_count"] == 0
    assert "filing_fetched" not in [e["event"] for e in events]


@pytest.mark.parametrize("status", [403, 404])
def test_scan_daily_index_skips_missing_daily_index(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    """EDGAR returns 403 for missing/unpublished daily indexes (non-business
    days, future dates, today before ~10 PM ET). 404 is the other
    not-present idiom. Both must skip + continue, not fail the tick."""
    monkeypatch.setattr(
        "filings_orchestrator.cli.scan_daily_index.classify_filing",
        _stub_classify_filing,
    )
    fixed_now = datetime(2026, 5, 19, 20, 0, 0, tzinfo=UTC)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return fixed_now if tz is None else fixed_now.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr("filings_orchestrator.cli.scan_daily_index.datetime", _FixedDateTime)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR2/master.20260519.idx"
        ).mock(return_value=httpx.Response(status))

        main()

    events = _read_jsonl(capsys.readouterr().out)
    event_names = [e["event"] for e in events]
    assert "tick_failed" not in event_names
    assert "tick_completed" in event_names

    skipped = next(e for e in events if e["event"] == "tick_skipped_date")
    assert skipped["date"] == "2026-05-19"
    assert skipped["status"] == status


def test_dates_to_scan_returns_today_when_cursor_unset() -> None:
    today = date(2026, 5, 19)
    assert _dates_to_scan(None, today_et=today) == [today]


def test_dates_to_scan_walks_from_cursor_through_today() -> None:
    today = date(2026, 5, 19)
    out = _dates_to_scan("20260515", today_et=today)
    assert out == [
        date(2026, 5, 15),
        date(2026, 5, 16),
        date(2026, 5, 17),
        date(2026, 5, 18),
        date(2026, 5, 19),
    ]


def test_dates_to_scan_clamps_to_today_if_cursor_in_future() -> None:
    """Clock skew or operator hand-edit could leave the cursor ahead of
    today. Defensive: just scan today."""
    today = date(2026, 5, 19)
    out = _dates_to_scan("20260601", today_et=today)
    assert out == [today]


def test_scan_daily_index_exits_with_cost_cap_exceeded_above_cap(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When today's aggregated cost is at or above the configured cap, the tick
    refuses to do any new LLM-bound work (ADR 0029). The check fires before
    EDGAR is contacted, so no respx mocks are required."""
    # Lower the cap so a small seeded value crosses it; the warn level stays
    # below the cap.
    monkeypatch.setenv("ANTHROPIC_DAILY_COST_CAP_USD", "0.50")
    monkeypatch.setenv("ANTHROPIC_DAILY_COST_WARN_USD", "0.25")

    # Seed a cost row dated to "today UTC" that exceeds the cap.
    today_iso = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00+00:00")
    engine = open_engine(str(configured_env))
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO llm_calls (
                    emitted_at, model, stage, accession_number,
                    input_tokens, output_tokens, estimated_cost_usd
                ) VALUES (
                    :emitted_at, :model, 'classify', NULL, 0, 0, :cost
                )
                """
            ),
            {"emitted_at": today_iso, "model": "claude-haiku-4-5-20251001", "cost": 0.75},
        )

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1

    events = _read_jsonl(capsys.readouterr().out)
    failure_events = [e for e in events if e["event"] == "tick_failed"]
    assert len(failure_events) == 1
    assert failure_events[0]["error_class"] == "cost_cap_exceeded"
    assert failure_events[0]["cap_usd"] == 0.50
    assert failure_events[0]["daily_spend_usd"] == 0.75
    # Crucially, the cap check fires before classification starts — no
    # filings should have been touched.
    assert "filing_fetched" not in [e["event"] for e in events]


def test_scan_daily_index_emits_cost_warning_between_warn_and_cap(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Between warn and cap, the tick proceeds but emits a structured
    cost_warning event so the operator sees the approach to the cap."""
    monkeypatch.setattr(
        "filings_orchestrator.cli.scan_daily_index.classify_filing",
        _stub_classify_filing,
    )
    monkeypatch.setenv("ANTHROPIC_DAILY_COST_CAP_USD", "1.00")
    monkeypatch.setenv("ANTHROPIC_DAILY_COST_WARN_USD", "0.50")

    # The CLI's datetime is monkeypatched below to a fixed_now of 2026-05-15;
    # the seed row must share that UTC day for the pre-tick check to see it.
    today_iso = "2026-05-15T12:00:00+00:00"
    engine = open_engine(str(configured_env))
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO llm_calls (
                    emitted_at, model, stage, accession_number,
                    input_tokens, output_tokens, estimated_cost_usd
                ) VALUES (
                    :emitted_at, :model, 'classify', NULL, 0, 0, :cost
                )
                """
            ),
            {"emitted_at": today_iso, "model": "claude-haiku-4-5-20251001", "cost": 0.75},
        )

    fixed_now = datetime(2026, 5, 15, 16, 0, 0, tzinfo=UTC)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return fixed_now if tz is None else fixed_now.astimezone(tz)  # type: ignore[arg-type]

    monkeypatch.setattr("filings_orchestrator.cli.scan_daily_index.datetime", _FixedDateTime)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(
            "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR2/master.20260515.idx"
        ).mock(return_value=httpx.Response(404))
        main()

    events = _read_jsonl(capsys.readouterr().out)
    warnings = [e for e in events if e["event"] == "cost_warning"]
    assert len(warnings) == 1
    assert warnings[0]["warn_usd"] == 0.50
    assert warnings[0]["cap_usd"] == 1.00
    assert warnings[0]["daily_spend_usd"] == 0.75
    # The tick still completed — the warning does not block work.
    assert "tick_completed" in [e["event"] for e in events]
    assert "tick_failed" not in [e["event"] for e in events]
