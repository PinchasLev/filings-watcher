"""Tests for the runs ledger and the filing-level events layer (ADR 0027/0028).

In-memory SQLite, fresh engine per test, on-disk migrations applied. The
central case is `test_latest_run_per_filing_drops_orphan`, which proves the
run-based "current view" selection: a smaller, newer reduce run must not leave
an orphaned event surfacing from a larger, older one.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import Engine, text

from filings_orchestrator.classify import (
    Classification,
    EventType,
    FilingClassification,
    FilingEvents,
    ItemClassification,
    ReducedEvent,
    domain_for,
)
from filings_orchestrator.edgar.models import Filing, FilingItem
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    complete_run,
    create_run,
    insert_classifications,
    insert_events,
    latest_run_events_for_filing,
    upsert_filing,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()

ACCESSION = "0001922446-26-000004"


def _fresh_db() -> Engine:
    engine = open_engine(":memory:")
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    return engine


def _filing() -> Filing:
    return Filing(
        cik="0001922446",
        company_name="Diversified Energy Co",
        ticker="DEC",
        form="8-K",
        accession_number=ACCESSION,
        filing_date=date(2026, 5, 21),
        report_date=None,
        primary_document="dec.htm",
        primary_document_url=(
            "https://www.sec.gov/Archives/edgar/data/1922446/000192244626000004/dec.htm"
        ),
        items=[FilingItem(number=n) for n in ("1.01", "2.03", "5.02", "7.01")],
    )


def _classifications() -> FilingClassification:
    def _item(number: str, event_value: str, material: bool) -> ItemClassification:
        return ItemClassification(
            item_number=number,
            item_title=None,
            classification=Classification(
                event_type=EventType(event_value),
                is_material=material,
                confidence=0.9,
                reasoning=f"Item {number}.",
            ),
        )

    return FilingClassification(
        accession_number=ACCESSION,
        cik="0001922446",
        company_name="Diversified Energy Co",
        filing_date="2026-05-21",
        items=[
            _item("1.01", "ma_activity", True),
            _item("2.03", "other_material", False),
            _item("5.02", "exec_appointment", True),
            _item("7.01", "exec_appointment", True),
        ],
        whole_filing=None,
        classified_at=datetime(2026, 5, 22, 0, 0, tzinfo=UTC),
        model="haiku-4.5",
        classifier_version="haiku-4.5+prompt-aaaa1111",
        taxonomy_version="v1",
    )


def _events_under_merged() -> FilingEvents:
    """An earlier run that fails to merge 7.01 into the 5.02 appointment: 3 events."""
    return FilingEvents(
        accession_number=ACCESSION,
        events=[
            ReducedEvent(
                event_type=EventType("ma_activity"),
                is_material=True,
                confidence=0.85,
                summary="ABS notes issuance.",
                anchor_item_number="1.01",
                contributing_item_numbers=["1.01", "2.03"],
            ),
            ReducedEvent(
                event_type=EventType("exec_appointment"),
                is_material=True,
                confidence=0.9,
                summary="Oliver appointment.",
                anchor_item_number="5.02",
                contributing_item_numbers=["5.02"],
            ),
            ReducedEvent(
                event_type=EventType("exec_appointment"),
                is_material=True,
                confidence=0.6,
                summary="FD furnishing of the appointment (not yet merged).",
                anchor_item_number="7.01",
                contributing_item_numbers=["7.01"],
            ),
        ],
    )


def _events_merged() -> FilingEvents:
    """A later run that merges correctly: 2 events, no standalone 7.01."""
    return FilingEvents(
        accession_number=ACCESSION,
        events=[
            ReducedEvent(
                event_type=EventType("ma_activity"),
                is_material=True,
                confidence=0.9,
                summary="ABS notes issuance; 2.03 obligation reconciled to 1.01.",
                anchor_item_number="1.01",
                contributing_item_numbers=["1.01", "2.03"],
            ),
            ReducedEvent(
                event_type=EventType("exec_appointment"),
                is_material=True,
                confidence=0.95,
                summary="Oliver appointment; 7.01 furnishing merged in.",
                anchor_item_number="5.02",
                contributing_item_numbers=["5.02", "7.01"],
            ),
        ],
    )


def _seed_filing_and_classifications(engine: Engine) -> None:
    upsert_filing(engine, _filing())
    insert_classifications(engine, _classifications())


def test_migration_004_creates_runs_and_events_tables() -> None:
    engine = _fresh_db()
    with engine.begin() as conn:
        names = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))
        }
    assert {"runs", "events", "event_classifications"} <= names


def test_create_and_complete_run() -> None:
    engine = _fresh_db()
    run_id = create_run(
        engine,
        stage="reduce",
        config_version="reducer+aaaa1111",
        taxonomy_version="v1",
        model="haiku-4.5",
    )
    assert run_id > 0
    complete_run(engine, run_id, status="succeeded")
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT stage, status, finished_at FROM runs WHERE run_id = :r"),
            {"r": run_id},
        ).fetchone()
    assert row is not None
    assert row[0] == "reduce"
    assert row[1] == "succeeded"
    assert row[2] is not None


def test_each_run_is_a_new_run_id_even_with_identical_config() -> None:
    """ADR 0028: every deliberate re-run is a new run; no dedup on config_version,
    because the LLM may produce different output under identical configuration."""
    engine = _fresh_db()
    r1 = create_run(engine, stage="reduce", config_version="same", taxonomy_version="v1")
    r2 = create_run(engine, stage="reduce", config_version="same", taxonomy_version="v1")
    assert r1 != r2


def test_insert_events_writes_rows_and_links_contributing_classifications() -> None:
    engine = _fresh_db()
    _seed_filing_and_classifications(engine)
    run_id = create_run(
        engine, stage="reduce", config_version="reducer+aaaa", taxonomy_version="v1"
    )

    inserted = insert_events(engine, _events_merged(), run_id=run_id)
    assert inserted == 2

    rows = latest_run_events_for_filing(engine, ACCESSION)
    assert {r["anchor_item_number"] for r in rows} == {"1.01", "5.02"}

    # event_domain is derived from event_type, not supplied by the caller.
    by_anchor = {r["anchor_item_number"]: r for r in rows}
    assert by_anchor["5.02"]["event_domain"] == domain_for(EventType("exec_appointment")).value

    # The 5.02 appointment event links to both the 5.02 and 7.01 classifications.
    with engine.begin() as conn:
        event_id = conn.execute(
            text("SELECT id FROM events WHERE run_id = :r AND anchor_item_number = '5.02'"),
            {"r": run_id},
        ).scalar_one()
        linked = conn.execute(
            text(
                """
                SELECT c.item_number
                  FROM event_classifications ec
                  JOIN classifications c ON c.id = ec.classification_id
                 WHERE ec.event_id = :e
                """
            ),
            {"e": event_id},
        ).fetchall()
    assert {row[0] for row in linked} == {"5.02", "7.01"}


def test_insert_events_is_idempotent_within_a_run() -> None:
    engine = _fresh_db()
    _seed_filing_and_classifications(engine)
    run_id = create_run(
        engine, stage="reduce", config_version="reducer+aaaa", taxonomy_version="v1"
    )

    assert insert_events(engine, _events_merged(), run_id=run_id) == 2
    # Re-running the same run (e.g. a resumed/retried pass) writes nothing new.
    assert insert_events(engine, _events_merged(), run_id=run_id) == 0


def test_latest_run_per_filing_drops_orphan() -> None:
    """The core ADR 0028 guarantee. An earlier run emits 3 events (including a
    standalone 7.01); a later run emits 2 (7.01 merged away). The current view
    is the later run's set in full — the 7.01 orphan does not survive."""
    engine = _fresh_db()
    _seed_filing_and_classifications(engine)

    run_a = create_run(engine, stage="reduce", config_version="reducer+v1", taxonomy_version="v1")
    assert insert_events(engine, _events_under_merged(), run_id=run_a) == 3

    run_b = create_run(engine, stage="reduce", config_version="reducer+v2", taxonomy_version="v1")
    assert insert_events(engine, _events_merged(), run_id=run_b) == 2

    rows = latest_run_events_for_filing(engine, ACCESSION)
    assert {r["anchor_item_number"] for r in rows} == {"1.01", "5.02"}
    assert all(r["run_id"] == run_b for r in rows)
