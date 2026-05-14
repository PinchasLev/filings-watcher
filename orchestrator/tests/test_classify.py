"""Tests for the 8-K classification graph.

Anthropic API calls are mocked at the `ChatAnthropic.invoke` level so no
network or API key is needed in CI.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any
from unittest.mock import patch

import pytest

from filings_orchestrator.classify import (
    EVENT_TO_DOMAIN,
    Classification,
    EventDomain,
    EventType,
    classify_filing,
    domain_for,
)
from filings_orchestrator.classify.classifier import _build_user_message
from filings_orchestrator.classify.taxonomy import (
    EVENT_TYPE_DESCRIPTIONS,
    NON_SUBSTANTIVE_ITEMS,
)
from filings_orchestrator.edgar.document import FilingDocument, ItemSection
from filings_orchestrator.edgar.models import Filing


def _filing() -> Filing:
    return Filing(
        cik="0000320193",
        company_name="Apple Inc.",
        ticker="AAPL",
        form="8-K",
        accession_number="0000320193-26-000045",
        filing_date=date(2026, 4, 30),
        report_date=date(2026, 4, 30),
        primary_document="aapl-20260430.htm",
        primary_document_url=(
            "https://www.sec.gov/Archives/edgar/data/320193/000032019326000045/aapl-20260430.htm"
        ),
    )


def _document(items: list[ItemSection], text: str = "Body text.") -> FilingDocument:
    return FilingDocument(
        filing=_filing(),
        text=text,
        items=items,
        raw_size_bytes=len(text.encode("utf-8")),
    )


class _ToolCallResponse:
    """Stand-in for ChatAnthropic responses that carry tool_calls."""

    def __init__(self, tool_args: dict[str, Any]) -> None:
        self.tool_calls = [
            {
                "name": "submit_classification",
                "args": tool_args,
                "id": "tool_call_test",
            }
        ]


def _mock_invocations(responses: list[Classification]) -> Iterator[_ToolCallResponse]:
    return iter(_ToolCallResponse(c.model_dump(mode="json")) for c in responses)


def test_taxonomy_descriptions_cover_every_event_type() -> None:
    """Every EventType must carry a description for the system prompt."""
    for event in EventType:
        assert event in EVENT_TYPE_DESCRIPTIONS, f"missing description for {event}"
        assert EVENT_TYPE_DESCRIPTIONS[event], f"empty description for {event}"


def test_non_substantive_items_includes_exhibits_only() -> None:
    """Item 9.01 is pure scaffolding; skipping it keeps the classifier focused."""
    assert "9.01" in NON_SUBSTANTIVE_ITEMS


def test_every_event_type_has_a_domain_mapping() -> None:
    """The post-hoc domain mapping must cover every EventType. Drift here would
    silently send some classifications to a KeyError at runtime."""
    for event in EventType:
        assert event in EVENT_TO_DOMAIN, f"missing domain mapping for {event}"


def test_every_domain_has_at_least_one_event_type() -> None:
    """Every declared EventDomain should be reachable from at least one
    EventType. An orphan domain is a sign of dead taxonomy structure."""
    domains_used = set(EVENT_TO_DOMAIN.values())
    for domain in EventDomain:
        assert domain in domains_used, f"unused EventDomain: {domain}"


def test_domain_for_returns_expected_domain() -> None:
    """Spot-check a few mappings to lock in the intended grouping."""
    assert domain_for(EventType.EXEC_DEPARTURE) == EventDomain.GOVERNANCE
    assert domain_for(EventType.EARNINGS_RELEASE) == EventDomain.FINANCIAL
    assert domain_for(EventType.MA_ACTIVITY) == EventDomain.OPERATIONAL
    assert domain_for(EventType.MATERIAL_LITIGATION) == EventDomain.LEGAL
    assert domain_for(EventType.BANKRUPTCY_FILING) == EventDomain.TERMINAL
    assert domain_for(EventType.OTHER_MATERIAL) == EventDomain.CATCHALL


def test_build_user_message_includes_metadata_and_section_text() -> None:
    """The model needs filing context (company, date, form) plus the section text."""
    item = ItemSection(number="5.02", title="Departure of Directors", text="Jane Doe resigned.")
    doc = _document([item])
    user = _build_user_message(doc, item)
    assert "Apple Inc." in user
    assert "2026-04-30" in user
    assert "Item 5.02: Departure of Directors" in user
    assert "Jane Doe resigned." in user


def test_build_user_message_handles_no_items_fallback() -> None:
    """When no Items are extractable, the whole-filing message uses the body text."""
    doc = _document(items=[], text="Full filing body without item headers.")
    user = _build_user_message(doc, None)
    assert "No Item sections were extractable" in user
    assert "Full filing body without item headers." in user


def test_classify_filing_classifies_each_substantive_item() -> None:
    """Each substantive Item gets its own classification call."""
    items = [
        ItemSection(number="2.02", title="Results of Operations", text="Earnings release."),
        ItemSection(number="5.02", title="Departure of Directors", text="Jane Doe resigned."),
        ItemSection(number="9.01", title="Financial Statements and Exhibits", text="Exhibits."),
    ]
    doc = _document(items)
    fake_responses = [
        Classification(
            event_type=EventType.EARNINGS_RELEASE,
            is_material=True,
            confidence=0.95,
            reasoning="Press release announcing quarterly results.",
        ),
        Classification(
            event_type=EventType.EXEC_DEPARTURE,
            is_material=True,
            confidence=0.92,
            reasoning="CFO resignation.",
        ),
    ]
    response_iter = _mock_invocations(fake_responses)

    with patch("filings_orchestrator.classify.classifier.ChatAnthropic") as mock_chat:
        mock_instance = mock_chat.return_value
        bound = mock_instance.bind_tools.return_value
        bound.invoke.side_effect = lambda _messages: next(response_iter)

        result = classify_filing(doc)

    # Item 9.01 was skipped — only the substantive items were classified.
    assert [ic.item_number for ic in result.items] == ["2.02", "5.02"]
    assert result.items[0].classification.event_type == EventType.EARNINGS_RELEASE
    assert result.items[1].classification.event_type == EventType.EXEC_DEPARTURE
    assert result.whole_filing is None
    assert result.accession_number == "0000320193-26-000045"


def test_classify_filing_falls_back_to_whole_filing_when_no_items() -> None:
    """No extractable Items → one whole-filing classification."""
    doc = _document(items=[], text="A material event description with no item headings.")
    fake_classification = Classification(
        event_type=EventType.OTHER_MATERIAL,
        is_material=True,
        confidence=0.7,
        reasoning="Material event described without standard Item formatting.",
    )
    response_iter = _mock_invocations([fake_classification])

    with patch("filings_orchestrator.classify.classifier.ChatAnthropic") as mock_chat:
        mock_instance = mock_chat.return_value
        bound = mock_instance.bind_tools.return_value
        bound.invoke.side_effect = lambda _messages: next(response_iter)

        result = classify_filing(doc)

    assert result.items == []
    assert result.whole_filing is not None
    assert result.whole_filing.event_type == EventType.OTHER_MATERIAL


def test_classify_filing_raises_when_model_does_not_call_tool() -> None:
    """If Claude returns no tool call (shouldn't happen with tool_choice forced),
    we surface a clear error rather than silently producing nonsense."""

    class NoToolCallResponse:
        def __init__(self) -> None:
            self.tool_calls: list[Any] = []

    item = ItemSection(number="5.02", title=None, text="x")
    doc = _document([item])

    with patch("filings_orchestrator.classify.classifier.ChatAnthropic") as mock_chat:
        mock_instance = mock_chat.return_value
        bound = mock_instance.bind_tools.return_value
        bound.invoke.return_value = NoToolCallResponse()

        with pytest.raises(RuntimeError, match="tool call"):
            classify_filing(doc)


def test_classify_filing_truncates_oversized_section_text() -> None:
    """Excessively long item text must be truncated before being sent to the model.

    Prevents one outlier filing from blowing up token usage. The classifier
    should still produce a valid result.
    """
    huge_text = "x" * 100_000
    item = ItemSection(number="2.02", title=None, text=huge_text)
    doc = _document([item])
    fake = Classification(
        event_type=EventType.EARNINGS_RELEASE,
        is_material=True,
        confidence=0.9,
        reasoning="ok",
    )

    captured_user_messages: list[str] = []

    with patch("filings_orchestrator.classify.classifier.ChatAnthropic") as mock_chat:
        mock_instance = mock_chat.return_value
        bound = mock_instance.bind_tools.return_value

        def capture(messages: list[Any]) -> _ToolCallResponse:
            captured_user_messages.append(messages[-1].content)
            return _ToolCallResponse(fake.model_dump(mode="json"))

        bound.invoke.side_effect = capture
        result = classify_filing(doc)

    assert len(result.items) == 1
    sent = captured_user_messages[0]
    # Truncation cap is 12_000; user message includes headers, so allow some
    # padding but well under the original 100k.
    assert len(sent) < 20_000
