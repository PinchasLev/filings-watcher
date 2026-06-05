"""End-to-end tests for the scan-atom-feed CLI (ADR 0029).

Live HTTP (EDGAR) is intercepted by respx. The Anthropic classifier is
monkeypatched at its module-level entry point so the test is fully
hermetic. The DB is a tmp_path SQLite file with migrations applied.

These tests cover the Atom-path-specific behaviors — single-fetch, no
cursor, atom_feed_polled event, dedup-via-PK-only — and rely on the
shared scan-daily-index test suite to cover the per-filing pipeline that
both paths share via `cli/_pipeline.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import text

from filings_orchestrator.classify.schema import Classification, FilingClassification
from filings_orchestrator.classify.taxonomy import EventType
from filings_orchestrator.cli.scan_atom_feed import main
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import read_ingest_cursor

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()
FIXTURES = Path(__file__).parent / "fixtures"

_ATOM_BODY = (FIXTURES / "atom_feed_cli_minimal.xml").read_text()
_FILING_INDEX_HTML = (FIXTURES / "filing_index_8k.html").read_text()

_FILING_BODY_HTML = """<html><body>
<p>Item 8.01 Other Events.</p>
<p>The Company announces a strategic shift.</p>
</body></html>
"""

_ATOM_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=8-K&output=atom&count=100"
)


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


def _stub_classify_filing(document: object) -> FilingClassification:
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
    return [json.loads(line) for line in captured.splitlines() if line.strip()]


def test_scan_atom_feed_classifies_new_8k_without_advancing_cursor(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Happy path: fetch the feed, process the single new entry through the
    shared pipeline, emit atom_feed_polled + tick_completed, and crucially
    do NOT touch the ingest cursor (the Atom path is cursor-less, ADR 0029).
    """
    monkeypatch.setattr(
        "filings_orchestrator.cli._pipeline.classify_filing",
        _stub_classify_filing,
    )

    with respx.mock(assert_all_called=True) as mock:
        mock.get(_ATOM_URL).mock(return_value=httpx.Response(200, text=_ATOM_BODY))
        mock.get(
            "https://www.sec.gov/Archives/edgar/data/101295/000117184326003455/"
            "0001171843-26-003455-index.html"
        ).mock(return_value=httpx.Response(200, text=_FILING_INDEX_HTML))
        mock.get(
            "https://www.sec.gov/Archives/edgar/data/101295/000117184326003455/f8k_051426.htm"
        ).mock(return_value=httpx.Response(200, text=_FILING_BODY_HTML))

        main()

    events = _read_jsonl(capsys.readouterr().out)
    names = [e["event"] for e in events]
    assert "tick_started" in names
    assert "atom_feed_polled" in names
    assert "filing_fetched" in names
    assert "classification_completed" in names
    assert "reduce_completed" in names
    assert "tick_completed" in names
    assert "tick_failed" not in names
    # The Atom path has no cursor and must not emit cursor_advanced.
    assert "cursor_advanced" not in names

    polled = next(e for e in events if e["event"] == "atom_feed_polled")
    assert polled["entries_total"] == 1
    assert polled["entries_new"] == 1

    started = next(e for e in events if e["event"] == "tick_started")
    assert started["source"] == "atom_feed"
    completed = next(e for e in events if e["event"] == "tick_completed")
    assert completed["source"] == "atom_feed"
    assert completed["new_filings_count"] == 1
    assert completed["errors_count"] == 0

    # Cursor must still be unset — the Atom path never touches it.
    engine = open_engine(str(configured_env))
    assert read_ingest_cursor(engine) is None

    # The 8-K is persisted exactly once.
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT accession_number, form FROM filings")).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "0001171843-26-003455"


def test_scan_atom_feed_dedups_against_already_seen_accession(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pre-seed the DB with the fixture's only accession; the tick must
    skip it (no filing-index or body fetch) and report entries_new=0."""
    # Pre-seed: insert a row for the accession the feed will surface. Only
    # the PK matters for dedup; the other NOT NULL columns are filled with
    # plausible placeholders.
    engine = open_engine(str(configured_env))
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO filings (
                    accession_number, cik, company_name, form, filing_date,
                    primary_document, primary_document_url, items_json, fetched_at
                ) VALUES (
                    '0001171843-26-003455', '0000101295', 'UNITED GUARDIAN INC',
                    '8-K', '2026-05-15', 'f8k_051426.htm',
                    'https://www.sec.gov/Archives/edgar/data/101295/000117184326003455/f8k_051426.htm',
                    '[]', '2026-05-15T16:00:00+00:00'
                )
                """
            )
        )

    monkeypatch.setattr(
        "filings_orchestrator.cli._pipeline.classify_filing",
        _stub_classify_filing,
    )

    with respx.mock(assert_all_called=True) as mock:
        # Only the Atom poll happens; no filing-index or body fetch because
        # the only entry is already in the DB.
        mock.get(_ATOM_URL).mock(return_value=httpx.Response(200, text=_ATOM_BODY))

        main()

    events = _read_jsonl(capsys.readouterr().out)
    names = [e["event"] for e in events]
    assert "atom_feed_polled" in names
    assert "filing_fetched" not in names
    assert "classification_started" not in names

    polled = next(e for e in events if e["event"] == "atom_feed_polled")
    assert polled["entries_total"] == 1
    assert polled["entries_new"] == 0

    completed = next(e for e in events if e["event"] == "tick_completed")
    assert completed["new_filings_count"] == 0


def test_scan_atom_feed_exits_when_daily_spend_at_cap(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """At or above the daily cap, the tick exits before any HTTP work — no
    feed fetch, no LLM calls. The ADR 0029 spend-cap discipline is deploy-
    gating on the Atom path because a runaway costs ~30x more per minute
    than the daily-index path."""
    # Seed llm_calls so daily_cost_usd returns a value >= cap (default 5.00
    # per env var, but configured_env doesn't set them so we rely on the
    # config default — set them explicitly here for a stable threshold).
    monkeypatch.setenv("ANTHROPIC_DAILY_COST_CAP_USD", "1.00")
    monkeypatch.setenv("ANTHROPIC_DAILY_COST_WARN_USD", "0.80")

    today_utc = datetime.now(UTC).date().isoformat()
    engine = open_engine(str(configured_env))
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO llm_calls (
                    emitted_at, model, input_tokens, output_tokens,
                    estimated_cost_usd, accession_number, stage
                ) VALUES (
                    :emitted, 'haiku-test', 1000, 500, 2.50,
                    '0000000000-26-000001', 'classify'
                )
                """
            ),
            {"emitted": f"{today_utc}T12:00:00Z"},
        )

    with pytest.raises(SystemExit) as excinfo:
        # No respx mock — the cap check must short-circuit before any HTTP.
        main()
    assert excinfo.value.code == 1

    events = _read_jsonl(capsys.readouterr().out)
    names = [e["event"] for e in events]
    assert "tick_started" in names
    assert "tick_failed" in names
    failed = next(e for e in events if e["event"] == "tick_failed")
    assert failed["error_class"] == "cost_cap_exceeded"
    assert "atom_feed_polled" not in names
