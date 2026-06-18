"""Tests for the exhibit-ab offline-eval harness.

Covers the deterministic diff/aggregate core (no LLM) and the sample-selection
repository query. The classify-both-and-report orchestration in main() is thin
glue over these tested pieces plus the already-tested classifier.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import Engine

from filings_orchestrator.classify.schema import (
    Classification,
    FilingClassification,
    ItemClassification,
)
from filings_orchestrator.classify.taxonomy import EventType
from filings_orchestrator.cli.exhibit_ab import _diff_filing, _summarize, _units
from filings_orchestrator.edgar.document import FilingDocument
from filings_orchestrator.edgar.models import Exhibit, Filing
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    list_exhibit_bearing_accessions,
    upsert_filing_document,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()


def _classification(
    *items: tuple[str, EventType, bool, float],
    whole: tuple[EventType, bool, float] | None = None,
) -> FilingClassification:
    return FilingClassification(
        accession_number="0000000005-26-000001",
        cik="0000000005",
        company_name="Test Co",
        filing_date="2026-06-16",
        items=[
            ItemClassification(
                item_number=num,
                item_title=None,
                classification=Classification(
                    event_type=et, is_material=mat, confidence=conf, reasoning="r"
                ),
            )
            for (num, et, mat, conf) in items
        ],
        whole_filing=(
            Classification(
                event_type=whole[0], is_material=whole[1], confidence=whole[2], reasoning="r"
            )
            if whole
            else None
        ),
        classified_at=datetime.now(UTC),
        model="m",
        classifier_version="m+prompt-1",
        taxonomy_version="v1",
    )


def test_units_flattens_items_and_whole_filing() -> None:
    c = _units(
        _classification(("7.01", EventType.OTHER_MATERIAL, True, 0.6), whole=None),
    )
    assert c == {"7.01": ("other_material", True, 0.6)}

    c2 = _units(_classification(whole=(EventType.OTHER_MATERIAL, False, 0.4)))
    assert c2 == {"__whole_filing__": ("other_material", False, 0.4)}


def test_diff_flags_event_type_change_and_confidence_delta() -> None:
    # With exhibits: a specific type, higher confidence. Without: other_material.
    with_ex = _classification(("7.01", EventType.MA_ACTIVITY, True, 0.9))
    without_ex = _classification(("7.01", EventType.OTHER_MATERIAL, True, 0.55))

    diff = _diff_filing("acc1", with_ex, without_ex)
    unit = diff["units"][0]
    assert unit["event_type_changed"] is True
    assert unit["with_event_type"] == "ma_activity"
    assert unit["without_event_type"] == "other_material"
    assert unit["confidence_delta"] == 0.35


def test_diff_marks_unit_present_in_one_arm_only() -> None:
    with_ex = _classification(("7.01", EventType.MA_ACTIVITY, True, 0.9))
    without_ex = _classification()  # no items, no whole
    diff = _diff_filing("acc1", with_ex, without_ex)
    assert diff["units"][0]["present_in"] == "with"


def test_summarize_counts_changes_and_other_material_reduction() -> None:
    results = [
        # exhibits flipped other_material -> ma_activity, +0.3 confidence
        _diff_filing(
            "a",
            _classification(("7.01", EventType.MA_ACTIVITY, True, 0.9)),
            _classification(("7.01", EventType.OTHER_MATERIAL, True, 0.6)),
        ),
        # exhibits made no difference
        _diff_filing(
            "b",
            _classification(("2.02", EventType.EARNINGS_RELEASE, True, 0.8)),
            _classification(("2.02", EventType.EARNINGS_RELEASE, True, 0.8)),
        ),
    ]
    s = _summarize(results)
    assert s["filings"] == 2
    assert s["comparable_units"] == 2
    assert s["event_type_changed"] == 1
    assert s["event_type_changed_pct"] == 50.0
    # one other_material in the without arm, zero in the with arm
    assert s["other_material_without_exhibits"] == 1
    assert s["other_material_with_exhibits"] == 0
    assert s["other_material_reduction"] == 1
    assert s["mean_confidence_delta"] == 0.15  # (0.3 + 0.0) / 2


def test_summarize_handles_empty() -> None:
    s = _summarize([])
    assert s["filings"] == 0
    assert s["comparable_units"] == 0
    assert s["event_type_changed_pct"] == 0
    assert s["mean_confidence_delta"] == 0


# --- sample-selection query ---


def _fresh_db() -> Engine:
    engine = open_engine(":memory:")
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    return engine


def _doc(accession: str, *, exhibits: list[Exhibit]) -> FilingDocument:
    filing = Filing(
        cik="0000000005",
        company_name="Test Co",
        form="8-K",
        accession_number=accession,
        filing_date=date(2026, 6, 16),
        primary_document="f.htm",
        primary_document_url="https://example.test/f.htm",
    )
    return FilingDocument(filing=filing, text="body", exhibits=exhibits, raw_size_bytes=4)


def test_list_exhibit_bearing_selects_only_filings_with_exhibits_and_body() -> None:
    engine = _fresh_db()
    ex = [Exhibit(exhibit_type="EX-99.1", document="ex1.htm", url="u", text="Release.")]
    upsert_filing_document(engine, _doc("0000000005-26-000001", exhibits=ex))
    upsert_filing_document(engine, _doc("0000000005-26-000002", exhibits=[]))  # no exhibits

    got = list_exhibit_bearing_accessions(engine)
    assert got == ["0000000005-26-000001"]


def test_list_exhibit_bearing_respects_limit() -> None:
    engine = _fresh_db()
    ex = [Exhibit(exhibit_type="EX-99.1", document="ex1.htm", url="u", text="x")]
    for i in range(3):
        upsert_filing_document(engine, _doc(f"0000000005-26-00000{i}", exhibits=ex))
    assert len(list_exhibit_bearing_accessions(engine, limit=2)) == 2
