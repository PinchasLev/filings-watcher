"""Tests for the reduce stage (ADR 0027/0028).

The Anthropic call is mocked at `reducer.ChatAnthropic`, mirroring the
classifier tests, so no network or API key is needed. The mocked golden case
verifies the plumbing — that a model response maps to FilingEvents with the
right anchors and contributing Items, and that invented Item numbers are
dropped. It does NOT verify the model's live merging judgment; that is checked
manually against the real filing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

from filings_orchestrator.classify import (
    Classification,
    EventType,
    FilingClassification,
    ItemClassification,
    SectionKind,
    reduce_filing,
    reducer_version,
)
from filings_orchestrator.classify.schema import ReducedEvent, ReduceOutput

ACCESSION = "0001922446-26-000004"

_REDUCER_PATCH = "filings_orchestrator.classify.reducer.ChatAnthropic"


class _ToolCallResponse:
    """Stand-in for a ChatAnthropic response carrying a submit_events tool call."""

    def __init__(self, tool_args: dict[str, Any]) -> None:
        self.tool_calls = [{"name": "submit_events", "args": tool_args, "id": "tc_test"}]


def _item(
    number: str,
    event_value: str,
    material: bool,
    reasoning: str,
    section_kind: SectionKind = SectionKind.EVENT,
) -> ItemClassification:
    return ItemClassification(
        item_number=number,
        item_title=None,
        classification=Classification(
            event_type=EventType(event_value),
            is_material=material,
            confidence=0.9,
            reasoning=reasoning,
            section_kind=section_kind,
        ),
    )


def _classification(items: list[ItemClassification]) -> FilingClassification:
    return FilingClassification(
        accession_number=ACCESSION,
        cik="0001922446",
        company_name="Diversified Energy Co",
        filing_date="2026-05-21",
        items=items,
        whole_filing=None,
        classified_at=datetime(2026, 5, 22, tzinfo=UTC),
        model="haiku-4.5",
        classifier_version="haiku-4.5+prompt-aaaa1111",
        taxonomy_version="v1",
    )


def _dec_classification() -> FilingClassification:
    return _classification(
        [
            _item("1.01", "ma_activity", True, "Entry into the ABS XII notes agreement."),
            _item(
                "2.03",
                "other_material",
                False,
                "Obligation incorporated by reference from Item 1.01.",
            ),
            _item("5.02", "exec_appointment", True, "Kirk Oliver appointed to the board."),
            _item("7.01", "exec_appointment", True, "Press release furnishing the appointment."),
        ]
    )


def _patched_reduce(classification: FilingClassification, output: ReduceOutput) -> Any:
    with patch(_REDUCER_PATCH) as mock_chat:
        bound = mock_chat.return_value.bind_tools.return_value
        bound.invoke.return_value = _ToolCallResponse(output.model_dump(mode="json"))
        return reduce_filing(classification)


def test_reduce_maps_model_events_and_resolves_references() -> None:
    """The DEC golden case: a 4-Item filing reduces to two events — the appointment
    (5.02 + 7.01 furnishing) and the financing (1.01 + 2.03 incorporated obligation)."""
    output = ReduceOutput(
        events=[
            ReducedEvent(
                event_type=EventType("ma_activity"),
                is_material=True,
                confidence=0.9,
                summary="ABS XII notes; the 2.03 obligation is the same notes.",
                anchor_item_number="1.01",
                contributing_item_numbers=["1.01", "2.03"],
            ),
            ReducedEvent(
                event_type=EventType("exec_appointment"),
                is_material=True,
                confidence=0.95,
                summary="Oliver appointment; the 7.01 furnishing is the same event.",
                anchor_item_number="5.02",
                contributing_item_numbers=["5.02", "7.01"],
            ),
        ]
    )

    result = _patched_reduce(_dec_classification(), output)

    assert result.accession_number == ACCESSION
    assert {e.anchor_item_number for e in result.events} == {"1.01", "5.02"}
    by_anchor = {e.anchor_item_number: e for e in result.events}
    assert set(by_anchor["5.02"].contributing_item_numbers) == {"5.02", "7.01"}
    assert set(by_anchor["1.01"].contributing_item_numbers) == {"1.01", "2.03"}
    assert by_anchor["5.02"].event_type == EventType("exec_appointment")


def test_reduce_grounds_out_invented_item_numbers() -> None:
    """An event referencing an Item that was never classified is re-grounded:
    the invented number is dropped and the anchor falls back to a real Item."""
    output = ReduceOutput(
        events=[
            ReducedEvent(
                event_type=EventType("ma_activity"),
                is_material=True,
                confidence=0.8,
                summary="References a hallucinated Item.",
                anchor_item_number="9.99",
                contributing_item_numbers=["9.99", "1.01"],
            ),
        ]
    )

    result = _patched_reduce(_dec_classification(), output)

    assert len(result.events) == 1
    event = result.events[0]
    assert event.anchor_item_number == "1.01"
    assert event.contributing_item_numbers == ["1.01"]


def _merger_classification() -> FilingClassification:
    """An ADIL-style merger 8-K: several Items all describing one transaction."""
    return _classification(
        [
            _item("1.01", "ma_activity", True, "Merger agreement entered into."),
            _item("2.01", "ma_activity", True, "Completion of the acquisition."),
            _item("5.03", "other_material", True, "Certificate of Designation for the merger."),
            _item("7.01", "other_material", True, "Press release furnishing the merger."),
        ]
    )


def test_reduce_drops_event_whose_items_are_subset_of_another() -> None:
    """The ADIL case: the model collates the merger into one event AND re-emits a
    standalone single-Item event for 5.03, whose Items are a subset of the merger.
    The subset event is dropped so the same classification isn't double-counted."""
    output = ReduceOutput(
        events=[
            ReducedEvent(
                event_type=EventType("ma_activity"),
                is_material=True,
                confidence=0.9,
                summary="The merger, collating every Item.",
                anchor_item_number="2.01",
                contributing_item_numbers=["1.01", "2.01", "5.03", "7.01"],
            ),
            ReducedEvent(
                event_type=EventType("other_material"),
                is_material=True,
                confidence=0.6,
                summary="Standalone Certificate of Designation — already part of the merger.",
                anchor_item_number="5.03",
                contributing_item_numbers=["5.03"],
            ),
        ]
    )

    result = _patched_reduce(_merger_classification(), output)

    assert len(result.events) == 1
    assert result.events[0].anchor_item_number == "2.01"
    assert set(result.events[0].contributing_item_numbers) == {"1.01", "2.01", "5.03", "7.01"}


def test_reduce_keeps_partially_overlapping_events() -> None:
    """Two events sharing one Item but neither nested in the other are both kept:
    a shared Item contributing to two distinct events is not over-emission."""
    output = ReduceOutput(
        events=[
            ReducedEvent(
                event_type=EventType("ma_activity"),
                is_material=True,
                confidence=0.9,
                summary="Financing.",
                anchor_item_number="1.01",
                contributing_item_numbers=["1.01", "7.01"],
            ),
            ReducedEvent(
                event_type=EventType("exec_appointment"),
                is_material=True,
                confidence=0.9,
                summary="Appointment, also furnished in the same 7.01 press release.",
                anchor_item_number="5.02",
                contributing_item_numbers=["5.02", "7.01"],
            ),
        ]
    )

    result = _patched_reduce(_dec_classification(), output)

    assert {e.anchor_item_number for e in result.events} == {"1.01", "5.02"}


def test_reduce_collapses_duplicate_events_with_identical_items() -> None:
    """Two events covering the exact same Item set are mutual subsets; the first
    is kept and the duplicate dropped."""
    output = ReduceOutput(
        events=[
            ReducedEvent(
                event_type=EventType("ma_activity"),
                is_material=True,
                confidence=0.9,
                summary="The financing, first emission.",
                anchor_item_number="1.01",
                contributing_item_numbers=["1.01", "2.03"],
            ),
            ReducedEvent(
                event_type=EventType("other_material"),
                is_material=True,
                confidence=0.5,
                summary="The same Items re-emitted under a different type.",
                anchor_item_number="2.03",
                contributing_item_numbers=["1.01", "2.03"],
            ),
        ]
    )

    result = _patched_reduce(_dec_classification(), output)

    assert len(result.events) == 1
    assert result.events[0].anchor_item_number == "1.01"
    assert result.events[0].event_type == EventType("ma_activity")


def test_reduce_single_item_is_passthrough_without_model_call() -> None:
    classification = _classification(
        [_item("2.02", "earnings_release", True, "Quarterly results.")]
    )
    with patch(_REDUCER_PATCH) as mock_chat:
        result = reduce_filing(classification)
        mock_chat.return_value.bind_tools.return_value.invoke.assert_not_called()
    assert len(result.events) == 1
    assert result.events[0].anchor_item_number == "2.02"
    assert result.events[0].contributing_item_numbers == ["2.02"]
    assert result.events[0].event_type == EventType("earnings_release")


def test_reduce_whole_filing_is_passthrough_without_model_call() -> None:
    classification = FilingClassification(
        accession_number=ACCESSION,
        cik="0001922446",
        company_name="Diversified Energy Co",
        filing_date="2026-05-21",
        items=[],
        whole_filing=Classification(
            event_type=EventType("other_material"),
            is_material=True,
            confidence=0.7,
            reasoning="No extractable Items; whole-filing classification.",
        ),
        classified_at=datetime(2026, 5, 22, tzinfo=UTC),
        model="haiku-4.5",
        classifier_version="haiku-4.5+prompt-aaaa1111",
        taxonomy_version="v1",
    )
    with patch(_REDUCER_PATCH) as mock_chat:
        result = reduce_filing(classification)
        mock_chat.return_value.bind_tools.return_value.invoke.assert_not_called()
    assert len(result.events) == 1
    assert result.events[0].anchor_item_number is None
    assert result.events[0].contributing_item_numbers == []


def test_reducer_version_format_and_stability() -> None:
    v1 = reducer_version("haiku-4.5")
    v2 = reducer_version("haiku-4.5")
    assert v1 == v2
    assert v1.startswith("haiku-4.5+reduce-")
    assert len(v1.rsplit("-", 1)[1]) == 8  # 8-char sha prefix


def test_reducer_version_differs_by_form() -> None:
    """The reduce prompt is form-specific, so 6-K carries a distinct version, and
    the 8-K default is unchanged."""
    assert reducer_version("haiku-4.5", form="6-K") != reducer_version("haiku-4.5", form="8-K")
    assert reducer_version("haiku-4.5") == reducer_version("haiku-4.5", form="8-K")


def _classification_6k(items: list[ItemClassification]) -> FilingClassification:
    return FilingClassification(
        accession_number="0001234567-26-000001",
        cik="0001234567",
        company_name="Foreign Issuer PLC",
        filing_date="2026-06-01",
        form="6-K",
        items=items,
        whole_filing=None,
        classified_at=datetime(2026, 6, 2, tzinfo=UTC),
        model="haiku-4.5",
        classifier_version="haiku-4.5+prompt-bbbb2222",
        taxonomy_version="v1",
    )


def test_reduce_6k_collates_exhibit_sections_into_events() -> None:
    """A 6-K's per-exhibit classifications reduce into events anchored on the
    exhibit labels; the grounding logic accepts exhibit keys unchanged."""
    classification = _classification_6k(
        [
            _item("EX-99.1", "earnings_release", True, "Half-year results."),
            _item("EX-99.2", "dividend_distribution", True, "Dividend declaration."),
        ]
    )
    output = ReduceOutput(
        events=[
            ReducedEvent(
                event_type=EventType("earnings_release"),
                is_material=True,
                confidence=0.9,
                summary="Half-year results.",
                anchor_item_number="EX-99.1",
                contributing_item_numbers=["EX-99.1"],
            ),
            ReducedEvent(
                event_type=EventType("dividend_distribution"),
                is_material=True,
                confidence=0.88,
                summary="Dividend declaration.",
                anchor_item_number="EX-99.2",
                contributing_item_numbers=["EX-99.2"],
            ),
        ]
    )
    result = _patched_reduce(classification, output)
    anchors = {e.anchor_item_number for e in result.events}
    assert anchors == {"EX-99.1", "EX-99.2"}


def test_reduce_6k_user_message_labels_exhibits() -> None:
    """The 6-K reduce prompt frames sections as exhibits, not Items."""
    from filings_orchestrator.classify.reducer import (
        _build_reduce_system_prompt,
        _build_reduce_user_message,
    )

    classification = _classification_6k(
        [_item("EX-99.1", "earnings_release", True, "Half-year results.")]
    )
    user = _build_reduce_user_message(classification)
    assert "Per-exhibit classifications:" in user
    assert "Exhibit EX-99.1" in user
    assert "Form: 6-K" in user
    system = _build_reduce_system_prompt("6-K")
    assert "per-exhibit" in system
    assert _build_reduce_system_prompt("6-K") != _build_reduce_system_prompt("8-K")


def test_reduce_excludes_periodic_sections() -> None:
    """A periodic_report section is deferred — not collated into events. A 6-K
    with one event exhibit + one periodic exhibit reduces to just the event,
    without a model call (only one event section remains)."""
    classification = _classification_6k(
        [
            _item("EX-99.1", "earnings_release", True, "Earnings press release."),
            _item(
                "EX-99.2",
                "other_material",
                False,
                "Interim financial statements.",
                section_kind=SectionKind.PERIODIC_REPORT,
            ),
        ]
    )
    with patch(_REDUCER_PATCH) as mock_chat:
        result = reduce_filing(classification)
        # One event section left → single-section fast path, no model call.
        mock_chat.return_value.bind_tools.return_value.invoke.assert_not_called()
    assert [e.anchor_item_number for e in result.events] == ["EX-99.1"]
    assert result.events[0].event_type == EventType("earnings_release")


def test_reduce_all_periodic_filing_yields_zero_events() -> None:
    """A 6-K whose only exhibits are periodic reports produces no events."""
    classification = _classification_6k(
        [
            _item(
                "EX-99.1",
                "other_material",
                False,
                "Annual financial statements.",
                section_kind=SectionKind.PERIODIC_REPORT,
            ),
            _item(
                "EX-99.2",
                "other_material",
                False,
                "MD&A.",
                section_kind=SectionKind.PERIODIC_REPORT,
            ),
        ]
    )
    with patch(_REDUCER_PATCH) as mock_chat:
        result = reduce_filing(classification)
        mock_chat.return_value.bind_tools.return_value.invoke.assert_not_called()
    assert result.events == []
