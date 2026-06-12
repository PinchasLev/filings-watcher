"""Tests for the over-emission detector (repository helper + CLI).

In-memory / tmp_path SQLite with migrations applied and seeded events. The
detector is read-only, so events are inserted directly (bypassing the reducer's
own subsumed-event drop) to simulate pre-fix data. The central case is a filing
whose latest run carries a standalone event whose Item set is a subset of a
larger event's — the ADIL over-emission shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine

from filings_orchestrator.classify import (
    Classification,
    EventType,
    FilingClassification,
    FilingEvents,
    ItemClassification,
    ReducedEvent,
)
from filings_orchestrator.cli.detect_over_emission import main
from filings_orchestrator.edgar.models import Filing, FilingItem
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    create_run,
    find_over_emitted_events,
    insert_classifications,
    insert_events,
    upsert_filing,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()

ACCESSION = "0001922446-26-000004"


def _seed(engine: Engine) -> None:
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


def _event(event_type: str, anchor: str, items: list[str], conf: float) -> ReducedEvent:
    return ReducedEvent(
        event_type=EventType(event_type),
        is_material=True,
        confidence=conf,
        summary=f"{anchor} event.",
        anchor_item_number=anchor,
        contributing_item_numbers=items,
    )


def _events_with_subset() -> FilingEvents:
    """A 5.02+7.01 appointment AND a standalone 7.01 whose Items are a subset."""
    return FilingEvents(
        accession_number=ACCESSION,
        events=[
            _event("exec_appointment", "5.02", ["5.02", "7.01"], 0.9),
            _event("exec_appointment", "7.01", ["7.01"], 0.6),
        ],
    )


def _events_clean() -> FilingEvents:
    """Two disjoint events — neither subsumes the other."""
    return FilingEvents(
        accession_number=ACCESSION,
        events=[
            _event("ma_activity", "1.01", ["1.01", "2.03"], 0.9),
            _event("exec_appointment", "5.02", ["5.02", "7.01"], 0.95),
        ],
    )


def _insert(engine: Engine, events: FilingEvents) -> int:
    run_id = create_run(
        engine, stage="reduce", config_version="reducer+aaaa", taxonomy_version="v1"
    )
    insert_events(engine, events, run_id=run_id)
    return run_id


def _fresh_db() -> Engine:
    engine = open_engine(":memory:")
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    return engine


def test_detects_subset_event() -> None:
    engine = _fresh_db()
    _seed(engine)
    _insert(engine, _events_with_subset())

    findings = find_over_emitted_events(engine)
    assert len(findings) == 1
    f = findings[0]
    assert f["accession_number"] == ACCESSION
    assert f["subsumed_anchor"] == "7.01"
    assert f["subsumed_items"] == ["7.01"]
    assert f["container_anchor"] == "5.02"
    assert f["container_items"] == ["5.02", "7.01"]


def test_clean_filing_has_no_findings() -> None:
    engine = _fresh_db()
    _seed(engine)
    _insert(engine, _events_clean())
    assert find_over_emitted_events(engine) == []


def test_only_scans_latest_run() -> None:
    """A subset emitted in an older run is invisible once a newer clean run lands —
    the detector mirrors the latest-run-wins current view (ADR 0028)."""
    engine = _fresh_db()
    _seed(engine)
    _insert(engine, _events_with_subset())  # older run, over-emitting
    _insert(engine, _events_clean())  # newer run, clean
    assert find_over_emitted_events(engine) == []


def test_accession_filter_scopes_the_scan() -> None:
    engine = _fresh_db()
    _seed(engine)
    _insert(engine, _events_with_subset())
    assert len(find_over_emitted_events(engine, ACCESSION)) == 1
    assert find_over_emitted_events(engine, "9999999999-99-999999") == []


def test_cli_exits_nonzero_when_findings_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "filings.db"
    monkeypatch.setenv("FILINGS_DB_PATH", str(db_path))
    engine = open_engine(str(db_path))
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    _seed(engine)
    _insert(engine, _events_with_subset())

    monkeypatch.setattr("sys.argv", ["detect-over-emission"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1


def test_cli_exits_zero_when_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "filings.db"
    monkeypatch.setenv("FILINGS_DB_PATH", str(db_path))
    engine = open_engine(str(db_path))
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    _seed(engine)
    _insert(engine, _events_clean())

    monkeypatch.setattr("sys.argv", ["detect-over-emission"])
    main()  # returns normally, no SystemExit
