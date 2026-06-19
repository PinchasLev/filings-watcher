"""Tests for the shared offline-eval core (cli/_eval.py).

Covers the deterministic diff/aggregate over two classification arms (baseline vs
candidate) — the core shared by exhibit-ab and classify-ab. No LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime

from filings_orchestrator.classify.schema import (
    Classification,
    FilingClassification,
    ItemClassification,
)
from filings_orchestrator.classify.taxonomy import EventType
from filings_orchestrator.cli._eval import diff_filing, summarize, units


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
    c = units(_classification(("7.01", EventType.OTHER_MATERIAL, True, 0.6)))
    assert c == {"7.01": ("other_material", True, 0.6)}

    c2 = units(_classification(whole=(EventType.OTHER_MATERIAL, False, 0.4)))
    assert c2 == {"__whole_filing__": ("other_material", False, 0.4)}


def test_diff_flags_event_type_change_and_confidence_delta() -> None:
    # Candidate moved a unit from the catch-all to a specific type, more confident.
    baseline = _classification(("7.01", EventType.OTHER_MATERIAL, True, 0.55))
    candidate = _classification(("7.01", EventType.MA_ACTIVITY, True, 0.9))

    unit = diff_filing("acc1", baseline, candidate)["units"][0]
    assert unit["event_type_changed"] is True
    assert unit["baseline_event_type"] == "other_material"
    assert unit["candidate_event_type"] == "ma_activity"
    assert unit["confidence_delta"] == 0.35  # candidate - baseline


def test_diff_marks_unit_present_in_one_arm_only() -> None:
    baseline = _classification()  # no items, no whole
    candidate = _classification(("7.01", EventType.MA_ACTIVITY, True, 0.9))
    unit = diff_filing("acc1", baseline, candidate)["units"][0]
    assert unit["present_in"] == "candidate"


def test_summarize_counts_changes_and_other_material_reduction() -> None:
    results = [
        # candidate flipped other_material -> ma_activity, +0.3 confidence
        diff_filing(
            "a",
            _classification(("7.01", EventType.OTHER_MATERIAL, True, 0.6)),
            _classification(("7.01", EventType.MA_ACTIVITY, True, 0.9)),
        ),
        # no difference
        diff_filing(
            "b",
            _classification(("2.02", EventType.EARNINGS_RELEASE, True, 0.8)),
            _classification(("2.02", EventType.EARNINGS_RELEASE, True, 0.8)),
        ),
    ]
    s = summarize(results)
    assert s["filings"] == 2
    assert s["comparable_units"] == 2
    assert s["event_type_changed"] == 1
    assert s["event_type_changed_pct"] == 50.0
    assert s["other_material_baseline"] == 1
    assert s["other_material_candidate"] == 0
    assert s["other_material_reduction"] == 1
    assert s["mean_confidence_delta"] == 0.15  # (0.3 + 0.0) / 2


def test_summarize_handles_empty() -> None:
    s = summarize([])
    assert s["filings"] == 0
    assert s["comparable_units"] == 0
    assert s["event_type_changed_pct"] == 0
    assert s["mean_confidence_delta"] == 0
