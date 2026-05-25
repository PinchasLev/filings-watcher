"""Tests for the reduce-corpus CLI.

reduce_filing is patched (no Anthropic call); the DB is a tmp_path SQLite file
with migrations applied and seeded classifications. The fixture sets only
ANTHROPIC_API_KEY and FILINGS_DB_PATH — deliberately NOT EDGAR_USER_AGENT — so
these tests also assert the CLI runs on the narrow config it actually needs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from filings_orchestrator.classify import (
    Classification,
    EventType,
    FilingClassification,
    FilingEvents,
    ItemClassification,
    ReducedEvent,
)
from filings_orchestrator.cli.reduce_corpus import main
from filings_orchestrator.edgar.models import Filing, FilingItem
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    insert_classifications,
    latest_run_events_for_filing,
    upsert_filing,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()

ACCESSION = "0001922446-26-000004"

_REDUCE_PATCH = "filings_orchestrator.cli.reduce_corpus.reduce_filing"


@pytest.fixture
def configured_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "filings.db"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("FILINGS_DB_PATH", str(db_path))
    apply_migrations(open_engine(str(db_path)), migrations_dir=MIGRATIONS_DIR)
    return db_path


def _seed_dec(db_path: Path) -> None:
    engine = open_engine(str(db_path))
    upsert_filing(
        engine,
        Filing(
            cik="0001922446",
            company_name="Diversified Energy Co",
            ticker="DEC",
            form="8-K",
            accession_number=ACCESSION,
            filing_date=datetime(2026, 5, 21).date(),
            report_date=None,
            primary_document="dec.htm",
            primary_document_url="https://www.sec.gov/Archives/edgar/data/1922446/x/dec.htm",
            items=[FilingItem(number=n) for n in ("1.01", "2.03", "5.02", "7.01")],
        ),
    )

    def _item(number: str, event_value: str) -> ItemClassification:
        return ItemClassification(
            item_number=number,
            item_title=None,
            classification=Classification(
                event_type=EventType(event_value),
                is_material=True,
                confidence=0.9,
                reasoning=f"Item {number}.",
            ),
        )

    insert_classifications(
        engine,
        FilingClassification(
            accession_number=ACCESSION,
            cik="0001922446",
            company_name="Diversified Energy Co",
            filing_date="2026-05-21",
            items=[
                _item("1.01", "ma_activity"),
                _item("2.03", "other_material"),
                _item("5.02", "exec_appointment"),
                _item("7.01", "exec_appointment"),
            ],
            whole_filing=None,
            classified_at=datetime(2026, 5, 22, tzinfo=UTC),
            model="haiku-4.5",
            classifier_version="haiku-4.5+prompt-aaaa1111",
            taxonomy_version="v1",
        ),
    )


def _two_events(accession: str) -> FilingEvents:
    return FilingEvents(
        accession_number=accession,
        events=[
            ReducedEvent(
                event_type=EventType("ma_activity"),
                is_material=True,
                confidence=0.9,
                summary="ABS notes; 2.03 reconciled to 1.01.",
                anchor_item_number="1.01",
                contributing_item_numbers=["1.01", "2.03"],
            ),
            ReducedEvent(
                event_type=EventType("exec_appointment"),
                is_material=True,
                confidence=0.95,
                summary="Oliver appointment; 7.01 furnishing merged.",
                anchor_item_number="5.02",
                contributing_item_numbers=["5.02", "7.01"],
            ),
        ],
    )


def test_reduce_corpus_single_accession_writes_events_and_run(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dec(configured_env)
    monkeypatch.setattr("sys.argv", ["reduce-corpus", "--accession", ACCESSION])

    with patch(_REDUCE_PATCH, return_value=_two_events(ACCESSION)) as mock_reduce:
        main()
    mock_reduce.assert_called_once()

    engine = open_engine(str(configured_env))
    events = latest_run_events_for_filing(engine, ACCESSION)
    assert {e["anchor_item_number"] for e in events} == {"1.01", "5.02"}

    with engine.begin() as conn:
        from sqlalchemy import text

        status = conn.execute(text("SELECT status FROM runs WHERE stage = 'reduce'")).scalar_one()
    assert status == "succeeded"


def test_reduce_corpus_all_filings(configured_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_dec(configured_env)
    # A second classified filing so corpus mode has more than one target.
    engine = open_engine(str(configured_env))
    other = "0000320193-26-000099"
    upsert_filing(
        engine,
        Filing(
            cik="0000320193",
            company_name="Apple Inc.",
            ticker="AAPL",
            form="8-K",
            accession_number=other,
            filing_date=datetime(2026, 5, 20).date(),
            report_date=None,
            primary_document="aapl.htm",
            primary_document_url="https://www.sec.gov/Archives/edgar/data/320193/x/aapl.htm",
            items=[FilingItem(number="2.02")],
        ),
    )
    insert_classifications(
        engine,
        FilingClassification(
            accession_number=other,
            cik="0000320193",
            company_name="Apple Inc.",
            filing_date="2026-05-20",
            items=[
                ItemClassification(
                    item_number="2.02",
                    item_title=None,
                    classification=Classification(
                        event_type=EventType("earnings_release"),
                        is_material=True,
                        confidence=0.95,
                        reasoning="Quarterly results.",
                    ),
                )
            ],
            whole_filing=None,
            classified_at=datetime(2026, 5, 21, tzinfo=UTC),
            model="haiku-4.5",
            classifier_version="haiku-4.5+prompt-aaaa1111",
            taxonomy_version="v1",
        ),
    )

    monkeypatch.setattr("sys.argv", ["reduce-corpus"])

    def _per_filing(classification: FilingClassification) -> FilingEvents:
        first = classification.items[0]
        return FilingEvents(
            accession_number=classification.accession_number,
            events=[
                ReducedEvent(
                    event_type=first.classification.event_type,
                    is_material=True,
                    confidence=0.9,
                    summary="one event",
                    anchor_item_number=first.item_number,
                    contributing_item_numbers=[first.item_number],
                )
            ],
        )

    with patch(_REDUCE_PATCH, side_effect=_per_filing) as mock_reduce:
        main()
    assert mock_reduce.call_count == 2

    assert len(latest_run_events_for_filing(engine, ACCESSION)) == 1
    assert len(latest_run_events_for_filing(engine, other)) == 1


def test_reduce_corpus_skips_filing_without_classifications(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["reduce-corpus", "--accession", "9999999999-99-999999"])
    with patch(_REDUCE_PATCH) as mock_reduce:
        main()  # returns normally (skip, not failure)
        mock_reduce.assert_not_called()

    engine = open_engine(str(configured_env))
    with engine.begin() as conn:
        from sqlalchemy import text

        run_count = conn.execute(text("SELECT COUNT(*) FROM runs")).scalar_one()
    assert run_count == 0
