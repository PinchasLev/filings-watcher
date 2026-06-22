"""Tests for 6-K classification: exhibits-as-sections and form-aware prompts.

A 6-K has no standardized Item structure — its substance is in the furnished
EX-99 exhibits — so the classifier treats each exhibit as one section (the
analogue of an 8-K Item), then the reduce stage collates them. Anthropic calls
are mocked at the `ChatAnthropic.invoke` level so no network or key is needed.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any
from unittest.mock import patch

from filings_orchestrator.classify import Classification, EventType, SectionKind, classify_filing
from filings_orchestrator.classify.classifier import (
    _MAX_6K_SECTION_CHARS,
    _MAX_SECTION_CHARS,
    _build_system_prompt,
    _build_user_message,
    _sections_for,
    classifier_version,
)
from filings_orchestrator.edgar.document import FilingDocument, ItemSection
from filings_orchestrator.edgar.models import Exhibit, Filing


def _filing_6k(exhibits: list[Exhibit]) -> Filing:
    return Filing(
        cik="0001234567",
        company_name="Foreign Issuer PLC",
        ticker=None,
        form="6-K",
        accession_number="0001234567-26-000001",
        filing_date=date(2026, 6, 1),
        report_date=None,
        primary_document="form6k.htm",
        primary_document_url=(
            "https://www.sec.gov/Archives/edgar/data/1234567/000123456726000001/form6k.htm"
        ),
        exhibits=exhibits,
    )


def _document_6k(exhibits: list[Exhibit], text: str = "Cover page.") -> FilingDocument:
    return FilingDocument(
        filing=_filing_6k(exhibits),
        text=text,
        items=[],
        exhibits=exhibits,
        raw_size_bytes=len(text.encode("utf-8")),
    )


def _exhibit(exhibit_type: str, document: str, text: str) -> Exhibit:
    return Exhibit(
        exhibit_type=exhibit_type,
        document=document,
        url=f"https://www.sec.gov/Archives/{document}",
        text=text,
    )


class _ToolCallResponse:
    def __init__(self, tool_args: dict[str, Any]) -> None:
        self.tool_calls = [
            {"name": "submit_classification", "args": tool_args, "id": "tool_call_test"}
        ]


def _mock_invocations(responses: list[Classification]) -> Iterator[_ToolCallResponse]:
    return iter(_ToolCallResponse(c.model_dump(mode="json")) for c in responses)


def test_sections_for_6k_uses_exhibits_not_items() -> None:
    """For a 6-K the sections are the furnished exhibits, keyed by exhibit label,
    and there is no separate shared context block."""
    exhibits = [
        _exhibit("EX-99.1", "pr.htm", "Half-year results announcement."),
        _exhibit("EX-99.2", "div.htm", "Dividend declaration."),
    ]
    sections, context = _sections_for(_document_6k(exhibits))
    assert [s.number for s in sections] == ["EX-99.1", "EX-99.2"]
    assert [s.text for s in sections] == [
        "Half-year results announcement.",
        "Dividend declaration.",
    ]
    assert context == ""


def test_sections_for_6k_disambiguates_duplicate_exhibit_types() -> None:
    """Two exhibits sharing a type get unique section keys so the persistence
    key (accession, item_number, classifier_version) stays unique."""
    exhibits = [
        _exhibit("EX-99", "a.htm", "Announcement A."),
        _exhibit("EX-99", "b.htm", "Announcement B."),
    ]
    sections, _ = _sections_for(_document_6k(exhibits))
    assert [s.number for s in sections] == ["EX-99", "EX-99#2"]


def test_classify_6k_classifies_each_exhibit() -> None:
    """Each furnished exhibit gets its own classification call; the section key
    is the exhibit label."""
    exhibits = [
        _exhibit("EX-99.1", "pr.htm", "Half-year results."),
        _exhibit("EX-99.2", "div.htm", "Dividend declared."),
    ]
    doc = _document_6k(exhibits)
    responses = [
        Classification(
            event_type=EventType.EARNINGS_RELEASE,
            is_material=True,
            confidence=0.9,
            reasoning="Interim results.",
        ),
        Classification(
            event_type=EventType.DIVIDEND_DISTRIBUTION,
            is_material=True,
            confidence=0.88,
            reasoning="Dividend declaration.",
        ),
    ]
    response_iter = _mock_invocations(responses)
    with patch("filings_orchestrator.classify.classifier.ChatAnthropic") as mock_chat:
        bound = mock_chat.return_value.bind_tools.return_value
        bound.invoke.side_effect = lambda _messages: next(response_iter)
        result = classify_filing(doc)

    assert result.form == "6-K"
    assert [ic.item_number for ic in result.items] == ["EX-99.1", "EX-99.2"]
    assert result.items[0].classification.event_type == EventType.EARNINGS_RELEASE
    assert result.items[1].classification.event_type == EventType.DIVIDEND_DISTRIBUTION
    assert result.whole_filing is None


def test_classify_6k_with_no_exhibits_falls_back_to_cover_body() -> None:
    """A 6-K furnishing no EX-99 exhibits classifies its cover body once."""
    doc = _document_6k(exhibits=[], text="The board announces a new strategic plan.")
    response_iter = _mock_invocations(
        [
            Classification(
                event_type=EventType.OTHER_MATERIAL,
                is_material=True,
                confidence=0.6,
                reasoning="Strategic announcement on the cover.",
            )
        ]
    )
    with patch("filings_orchestrator.classify.classifier.ChatAnthropic") as mock_chat:
        bound = mock_chat.return_value.bind_tools.return_value
        bound.invoke.side_effect = lambda _messages: next(response_iter)
        result = classify_filing(doc)

    assert result.items == []
    assert result.whole_filing is not None
    assert result.whole_filing.event_type == EventType.OTHER_MATERIAL


def test_build_user_message_labels_6k_section_as_exhibit() -> None:
    """The 6-K section header reads 'Exhibit ...', not 'Item ...'."""
    doc = _document_6k([_exhibit("EX-99.1", "pr.htm", "Results.")])
    section = ItemSection(number="EX-99.1", title="pr.htm", text="Results.")
    user = _build_user_message(doc, section)
    assert "Exhibit EX-99.1: pr.htm" in user
    assert "Form: 6-K" in user
    assert "Item EX-99.1" not in user


def test_classify_6k_propagates_periodic_section_kind() -> None:
    """When the model marks an exhibit a periodic financial report, the
    section_kind flows through to the persisted classification (ADR 0034)."""
    exhibits = [
        _exhibit("EX-99.1", "press.htm", "Q2 results press release."),
        _exhibit("EX-99.2", "financials.htm", "Condensed consolidated statements ..."),
    ]
    doc = _document_6k(exhibits)
    responses = [
        Classification(
            event_type=EventType.EARNINGS_RELEASE,
            is_material=True,
            confidence=0.9,
            reasoning="Earnings press release.",
            section_kind=SectionKind.EVENT,
        ),
        Classification(
            event_type=EventType.OTHER_MATERIAL,
            is_material=False,
            confidence=0.4,
            reasoning="Interim financial statements — periodic report.",
            section_kind=SectionKind.PERIODIC_REPORT,
        ),
    ]
    response_iter = _mock_invocations(responses)
    with patch("filings_orchestrator.classify.classifier.ChatAnthropic") as mock_chat:
        bound = mock_chat.return_value.bind_tools.return_value
        bound.invoke.side_effect = lambda _messages: next(response_iter)
        result = classify_filing(doc)

    kinds = {ic.item_number: ic.classification.section_kind for ic in result.items}
    assert kinds["EX-99.1"] == SectionKind.EVENT
    assert kinds["EX-99.2"] == SectionKind.PERIODIC_REPORT


def test_6k_prompt_instructs_periodic_recognition() -> None:
    """The 6-K prompt tells the model to defer periodic financial reports; the
    8-K prompt does not mention section_kind."""
    assert "periodic_report" in _build_system_prompt("6-K")
    assert "section_kind" in _build_system_prompt("6-K")
    assert "periodic_report" not in _build_system_prompt("8-K")


def test_periodic_signaled_via_event_type_is_routed() -> None:
    """Robustness: the model often signals periodic by putting 'periodic_report' in
    event_type (observed on real interim/annual 6-Ks). That out-of-taxonomy value is
    routed to section_kind with a placeholder event_type instead of crashing validation."""
    c = Classification.model_validate(
        {
            "event_type": "periodic_report",
            "is_material": False,
            "confidence": 0.6,
            "reasoning": "Interim condensed consolidated financial statements.",
        }
    )
    assert c.section_kind == SectionKind.PERIODIC_REPORT
    assert c.event_type == EventType.OTHER_MATERIAL


def test_section_kind_defaults_to_event() -> None:
    """A classification with no explicit section_kind is an event (8-K + back-compat)."""
    c = Classification(
        event_type=EventType.EARNINGS_RELEASE, is_material=True, confidence=0.9, reasoning="x"
    )
    assert c.section_kind == SectionKind.EVENT


def test_6k_section_uses_larger_char_budget() -> None:
    """A 6-K exhibit is the primary content, so its section budget exceeds the 8-K
    Item cap, and a long exhibit reaches the classifier well past 12k chars."""
    assert _MAX_6K_SECTION_CHARS > _MAX_SECTION_CHARS
    long_text = "X" * (_MAX_SECTION_CHARS + 20_000)
    doc = _document_6k([_exhibit("EX-99.1", "results.htm", long_text)])
    section = ItemSection(number="EX-99.1", title="results.htm", text=long_text)
    user = _build_user_message(doc, section)
    # Body included beyond the 8-K cap but bounded by the 6-K cap.
    assert user.count("X") > _MAX_SECTION_CHARS
    assert user.count("X") <= _MAX_6K_SECTION_CHARS


def test_6k_prompt_and_version_differ_from_8k() -> None:
    """The form-specific prompt yields a distinct classifier_version, while the
    8-K default is unchanged (byte-identical prompt)."""
    assert _build_system_prompt("6-K") != _build_system_prompt("8-K")
    assert "foreign private issuer" in _build_system_prompt("6-K")
    assert classifier_version(form="6-K") != classifier_version(form="8-K")
    # The default (8-K) version is the production string — keyword default holds.
    assert classifier_version() == classifier_version(form="8-K")
