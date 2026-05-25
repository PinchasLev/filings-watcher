"""Pydantic schemas for classification input and output.

`Classification` is the tool-call argument Claude returns for a single
classification request — type, confidence, reasoning, and an optional
materiality flag. `ItemClassification` and `FilingClassification` wrap
those results with the source metadata they belong to.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from filings_orchestrator.classify.taxonomy import EventType


class Classification(BaseModel):
    """One classification produced by the model.

    Matches the JSON Schema bound as a tool input. Field order and
    descriptions matter — they're what the model sees when deciding what
    to fill in.
    """

    event_type: EventType = Field(
        description="The single best matching event type from the taxonomy."
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
