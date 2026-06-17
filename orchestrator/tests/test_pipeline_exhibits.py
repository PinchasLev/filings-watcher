"""Pipeline-level tests for exhibit instrumentation in classify_and_reduce.

Asserts the deterministic side effects the pipeline adds around the (patched-out)
classifier: the `exhibit_context` metric event and the ALERT raised when the
volume budget drops adverse content. The classifier and reducer are patched so
no Anthropic call is made.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import Engine

from filings_orchestrator.alerting.outbox import fetch_undelivered_alerts
from filings_orchestrator.classify.schema import (
    Classification,
    FilingClassification,
)
from filings_orchestrator.classify.taxonomy import EventType
from filings_orchestrator.cli._pipeline import classify_and_reduce
from filings_orchestrator.edgar.document import FilingDocument
from filings_orchestrator.edgar.models import Exhibit, Filing
from filings_orchestrator.persistence import apply_migrations, open_engine

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()

_CLASSIFY = "filings_orchestrator.cli._pipeline.classify_filing"
_REDUCE = "filings_orchestrator.cli._pipeline.reduce_filing"


def _fresh_db() -> Engine:
    engine = open_engine(":memory:")
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    return engine


def _doc(*exhibits: Exhibit) -> FilingDocument:
    filing = Filing(
        cik="0000000005",
        company_name="Test Co",
        form="8-K",
        accession_number="0000000005-26-000001",
        filing_date=date(2026, 6, 16),
        primary_document="f.htm",
        primary_document_url="https://example.test/f.htm",
    )
    return FilingDocument(
        filing=filing,
        text="On June 16 the Company furnished a press release as Exhibit 99.1.",
        exhibits=list(exhibits),
        raw_size_bytes=64,
    )


def _stub_classification(accession: str) -> FilingClassification:
    return FilingClassification(
        accession_number=accession,
        cik="0000000005",
        company_name="Test Co",
        filing_date="2026-06-16",
        items=[],
        whole_filing=Classification(
            event_type=EventType.OTHER_MATERIAL,
            is_material=True,
            confidence=0.5,
            reasoning="stub",
        ),
        classified_at=datetime.now(UTC),
        model="haiku-test",
        classifier_version="haiku-test+prompt-abcd1234",
        taxonomy_version="v1-test",
    )


def _events(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_exhibit_context_metric_emitted(capsys: pytest.CaptureFixture[str]) -> None:
    engine = _fresh_db()
    doc = _doc(_ex := Exhibit(exhibit_type="EX-99.1", document="ex1.htm", url="u", text="Release."))

    with (
        patch(_CLASSIFY, return_value=_stub_classification(doc.filing.accession_number)),
        patch(_REDUCE, return_value=[]),
    ):
        classify_and_reduce(engine, doc)

    ctx = next(e for e in _events(capsys) if e["event"] == "exhibit_context")
    assert ctx["exhibit_count"] == 1
    assert ctx["truncated"] is False
    assert ctx["total_chars"] == len("Release.")


def test_no_exhibit_context_when_no_exhibits(capsys: pytest.CaptureFixture[str]) -> None:
    engine = _fresh_db()
    doc = _doc()  # no exhibits

    with (
        patch(_CLASSIFY, return_value=_stub_classification(doc.filing.accession_number)),
        patch(_REDUCE, return_value=[]),
    ):
        classify_and_reduce(engine, doc)

    assert "exhibit_context" not in [e["event"] for e in _events(capsys)]


def test_red_flag_in_truncated_tail_raises_alert(capsys: pytest.CaptureFixture[str]) -> None:
    engine = _fresh_db()
    benign = "Record revenue this quarter. " * 4
    buried = benign + " The company disclosed a going concern doubt."
    doc = _doc(Exhibit(exhibit_type="EX-99.1", document="ex1.htm", url="u", text=buried))

    # Force truncation right after the benign lede so the red flag lands in the tail.
    with (
        patch(_CLASSIFY, return_value=_stub_classification(doc.filing.accession_number)),
        patch(_REDUCE, return_value=[]),
        patch("filings_orchestrator.cli._pipeline.render_exhibits") as render,
    ):
        from filings_orchestrator.classify.exhibits import render_exhibits as real

        render.side_effect = lambda d: real(d, budget=len(benign))
        classify_and_reduce(engine, doc)

    alerts = fetch_undelivered_alerts(engine)
    flag_alerts = [a for a in alerts if a.title == "Adverse content truncated from exhibit"]
    assert len(flag_alerts) == 1
    assert flag_alerts[0].severity == "alert"
    assert "going concern" in flag_alerts[0].fields["terms"]


def test_no_alert_when_truncated_tail_is_clean(capsys: pytest.CaptureFixture[str]) -> None:
    engine = _fresh_db()
    text = "Routine quarterly update with no adverse terms whatsoever. " * 5
    doc = _doc(Exhibit(exhibit_type="EX-99.1", document="ex1.htm", url="u", text=text))

    with (
        patch(_CLASSIFY, return_value=_stub_classification(doc.filing.accession_number)),
        patch(_REDUCE, return_value=[]),
        patch("filings_orchestrator.cli._pipeline.render_exhibits") as render,
    ):
        from filings_orchestrator.classify.exhibits import render_exhibits as real

        render.side_effect = lambda d: real(d, budget=20)  # forces truncation
        classify_and_reduce(engine, doc)

    titles = [a.title for a in fetch_undelivered_alerts(engine)]
    assert "Adverse content truncated from exhibit" not in titles
