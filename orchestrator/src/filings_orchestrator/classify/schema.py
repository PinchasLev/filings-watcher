"""Pydantic schemas for classification input and output.

`Classification` is the tool-call argument Claude returns for a single
classification request — type, confidence, reasoning, and an optional
materiality flag. `ItemClassification` and `FilingClassification` wrap
those results with the source metadata they belong to.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from filings_orchestrator.classify.taxonomy import EventType


class SectionKind(StrEnum):
    """The document class of a classified section — distinct from its event type.

    A 6-K is a catch-all that carries both 8-K-equivalent events and the
    10-Q/10-K-equivalent periodic financial reports we deliberately defer
    (ADR 0033/0034). This is a document class, NOT a material-event type, so it
    is kept out of `EventType`/the taxonomy. `PERIODIC_REPORT` sections are
    recorded but not collated into events; they are the queryable hand-off for a
    future periodic-content extraction pass.
    """

    EVENT = "event"
    PERIODIC_REPORT = "periodic_report"


class Classification(BaseModel):
    """One classification produced by the model.

    Matches the JSON Schema bound as a tool input. Field order and
    descriptions matter — they're what the model sees when deciding what
    to fill in.
    """

    event_type: EventType = Field(
        description=(
            "The single best matching event type from the taxonomy below. Always "
            "one of the listed values — never 'periodic_report'; the periodic-vs-event "
            "distinction belongs in the separate section_kind field."
        )
    )
    is_material: bool = Field(
        description=(
            "True if this event is material to a reasonable investor — "
            "would affect the total mix of information available about the "
            "registrant. False for routine administrative disclosures."
        )
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Confidence in the event_type assignment on a 0..1 scale. Reflect "
            "genuine uncertainty: lower for ambiguous prose, higher when the "
            "language is unmistakable."
        ),
    )
    reasoning: str = Field(
        description=(
            "Brief explanation (1-3 sentences) of why the event_type was "
            "chosen. Reference specific phrases or named entities from the "
            "filing where relevant."
        )
    )
    section_kind: SectionKind = Field(
        default=SectionKind.EVENT,
        description=(
            "The document class of this section, separate from event_type. Use "
            "'periodic_report' ONLY when the section IS a periodic financial report — "
            "interim, half-year, or annual financial statements, MD&A, or a full "
            "results filing (the foreign-issuer equivalent of a 10-Q or 10-K). Such "
            "reports are deferred, not classified as a discrete event. Use 'event' for "
            "everything else, including a results press release (classify that as "
            "earnings_release, not periodic_report). When in doubt, choose 'event'."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _route_periodic_event_type(cls, data: Any) -> Any:
        """Route a periodic signal the model put in the wrong field.

        On real interim/annual 6-K filings the model often signals a periodic
        financial report by returning event_type='periodic_report' (an out-of-taxonomy
        value that would otherwise fail validation and crash the classify call). Since
        event_type only holds material-event taxonomy values, translate that into
        section_kind and a placeholder event_type (ignored — periodic sections are
        deferred, not collated into events; ADR 0034). A correctly-placed
        section_kind='periodic_report' is left untouched.
        """
        if isinstance(data, dict) and data.get("event_type") == SectionKind.PERIODIC_REPORT.value:
            data = {
                **data,
                "event_type": EventType.OTHER_MATERIAL.value,
                "section_kind": SectionKind.PERIODIC_REPORT.value,
            }
        return data


class ItemClassification(BaseModel):
    """Classification of one Item section within a filing."""

    item_number: str
    item_title: str | None
    classification: Classification


class FilingClassification(BaseModel):
    """Per-Item classifications for a single 8-K filing.

    When a filing has no extractable Item sections (the splitter found no
    headings), `items` is empty and `whole_filing` carries the single
    fallback classification of the entire body.
    """

    accession_number: str
    cik: str
    company_name: str
    filing_date: str
    # The SEC form this classification is for. Selects the form-specific reduce
    # prompt and version; defaults to "8-K" so existing 8-K rows/fixtures are
    # unaffected. For a 6-K the per-section keys are exhibit labels (e.g. "EX-99.1").
    form: str = "8-K"
    items: list[ItemClassification] = Field(default_factory=list)
    whole_filing: Classification | None = None
    classified_at: datetime
    model: str
    classifier_version: str
    taxonomy_version: str


class ReducedEvent(BaseModel):
    """One filing-level event produced by the reduce stage (ADR 0027/0028).

    The reduce stage collates a filing's per-Item classifications into
    deduplicated events. `anchor_item_number` is the primary substantive Item
    the event centers on and forms the event's within-run identity; companion
    Items (Reg-FD furnishings, exhibits, incorporations by reference) are listed
    in `contributing_item_numbers` but do not define the event.
    """

    event_type: EventType = Field(
        description="The single event type that best characterizes the consolidated event."
    )
    is_material: bool = Field(
        description="True if the consolidated event is material to a reasonable investor."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in the consolidated event_type on a 0..1 scale.",
    )
    summary: str = Field(
        description=(
            "A 1-3 sentence summary of the consolidated event, reconciling the "
            "contributing Items (including any incorporated-by-reference content)."
        )
    )
    anchor_item_number: str | None = Field(
        description=(
            "The primary substantive Item the event centers on (e.g. '5.02'); "
            "null when the filing had no extractable Items."
        )
    )
    contributing_item_numbers: list[str] = Field(
        default_factory=list,
        description=(
            "Every Item number this event subsumes, including the anchor — e.g. "
            "['5.02', '7.01'] when a 7.01 furnishing accompanies a 5.02 action."
        ),
    )


class FilingEvents(BaseModel):
    """The reduce output for one filing: its deduplicated, filing-level events.

    Run-level metadata (reducer version, timing, the source classify run) lives
    on the runs-ledger row, not here — this is only the per-filing payload.
    """

    accession_number: str
    events: list[ReducedEvent] = Field(default_factory=list)


class ReduceOutput(BaseModel):
    """Tool-call argument the reduce stage returns: a filing's consolidated events.

    The model does not echo the accession number — the caller pairs these events
    with the filing it reduced. Bound as the `submit_events` tool input schema.
    """

    events: list[ReducedEvent] = Field(
        default_factory=list,
        description="The distinct, consolidated events the filing discloses.",
    )
