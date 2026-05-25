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


def _item(number: str, event_value: str, material: bool, reasoning: str) -> ItemClassification:
    return ItemClassification(
        item_number=number,
        item_title=None,
        classification=Classification(
            event_type=EventType(event_value),
            is_material=material,
            confidence=0.9,
            reasoning=reasoning,
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
