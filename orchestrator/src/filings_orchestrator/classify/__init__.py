"""8-K filing classification: assign material event types to each Item section."""

from filings_orchestrator.classify.classifier import classify_filing
from filings_orchestrator.classify.schema import (
    Classification,
    FilingClassification,
    ItemClassification,
)
from filings_orchestrator.classify.taxonomy import (
    EVENT_TO_DOMAIN,
    EventDomain,
    EventType,
    domain_for,
)

__all__ = [
    "EVENT_TO_DOMAIN",
    "Classification",
    "EventDomain",
    "EventType",
    "FilingClassification",
    "ItemClassification",
    "classify_filing",
    "domain_for",
]
