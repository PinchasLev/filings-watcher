"""Pipeline-level tests for the classify-failure operator alert.

When a classification call propagates out of `classify_and_reduce` (it has
already exhausted in-call retries), the pipeline raises an ALERT to the outbox
so a halted pipeline — drained credit, bad key, sustained upstream outage — is
visible in Discord instead of only a `tick_failed` structured log. These tests
assert the categorization and the emit, with the classifier patched so no
Anthropic call is made.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from anthropic import (
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)
from sqlalchemy import Engine

from filings_orchestrator.alerting.outbox import fetch_undelivered_alerts
from filings_orchestrator.cli._pipeline import (
    _classify_failure_alert_params,
    classify_and_reduce,
)
from filings_orchestrator.edgar.document import FilingDocument
from filings_orchestrator.edgar.models import Filing
from filings_orchestrator.persistence import apply_migrations, open_engine

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()

_CLASSIFY = "filings_orchestrator.cli._pipeline.classify_filing"
_REDUCE = "filings_orchestrator.cli._pipeline.reduce_filing"


def _fresh_db() -> Engine:
    engine = open_engine(":memory:")
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    return engine


def _doc() -> FilingDocument:
    filing = Filing(
        cik="0000000005",
        company_name="Test Co",
        form="8-K",
        accession_number="0000000005-26-000001",
        filing_date=date(2026, 6, 16),
        primary_document="f.htm",
        primary_document_url="https://example.test/f.htm",
    )
    return FilingDocument(filing=filing, text="A material event.", raw_size_bytes=16)


def _resp(status: int) -> httpx.Response:
    return httpx.Response(
        status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )


def _credit_error() -> BadRequestError:
    return BadRequestError(
        "Your credit balance is too low to access the Anthropic API.",
        response=_resp(400),
        body=None,
    )


@pytest.mark.parametrize(
    ("exc", "cause"),
    [
        (_credit_error(), "anthropic_credit_exhausted"),
        (
            AuthenticationError("invalid x-api-key", response=_resp(401), body=None),
            "anthropic_auth_failed",
        ),
        (
            PermissionDeniedError("not allowed", response=_resp(403), body=None),
            "anthropic_auth_failed",
        ),
        (RateLimitError("slow down", response=_resp(429), body=None), "anthropic_upstream_outage"),
        (RuntimeError("something else"), "classify_unexpected_error"),
    ],
)
def test_classify_failure_categorization(exc: BaseException, cause: str) -> None:
    got_cause, title, body = _classify_failure_alert_params(exc)
    assert got_cause == cause
    assert title and body  # every category carries a human-readable headline + detail


def test_credit_badrequest_without_balance_phrase_is_not_credit() -> None:
    # A generic 400 (a real payload/prompt bug) must not masquerade as "out of
    # credit" — only the credit-balance message routes to that cause.
    exc = BadRequestError("messages.0: invalid role", response=_resp(400), body=None)
    cause, _, _ = _classify_failure_alert_params(exc)
    assert cause == "classify_unexpected_error"


def test_classify_failure_raises_alert_and_reraises() -> None:
    engine = _fresh_db()
    doc = _doc()

    with (
        patch(_CLASSIFY, side_effect=_credit_error()),
        patch(_REDUCE, return_value=[]),
        pytest.raises(BadRequestError),
    ):
        classify_and_reduce(engine, doc)

    alerts = fetch_undelivered_alerts(engine)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity == "alert"
    assert alert.title == "Anthropic credit exhausted — classification halted"
    assert alert.dedup_key == "classify_failure:anthropic_credit_exhausted"
    assert alert.fields["accession_number"] == doc.filing.accession_number
    assert alert.fields["error_class"] == "BadRequestError"


def test_no_alert_emitted_on_successful_classification() -> None:
    from datetime import UTC, datetime

    from filings_orchestrator.classify.schema import Classification, FilingClassification
    from filings_orchestrator.classify.taxonomy import EventType

    engine = _fresh_db()
    doc = _doc()
    stub = FilingClassification(
        accession_number=doc.filing.accession_number,
        cik="0000000005",
        company_name="Test Co",
        filing_date="2026-06-16",
        items=[],
        whole_filing=Classification(
            event_type=EventType.OTHER_MATERIAL, is_material=True, confidence=0.5, reasoning="s"
        ),
        classified_at=datetime.now(UTC),
        model="haiku-test",
        classifier_version="haiku-test+prompt-abcd1234",
        taxonomy_version="v1-test",
    )

    with patch(_CLASSIFY, return_value=stub), patch(_REDUCE, return_value=[]):
        classify_and_reduce(engine, doc)

    titles = [a.title for a in fetch_undelivered_alerts(engine)]
    assert not any(
        t.startswith("Anthropic") or t.startswith("Classification failing") for t in titles
    )
