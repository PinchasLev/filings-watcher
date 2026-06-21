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


def _doc(*exhibits: Exhibit, form: str = "8-K") -> FilingDocument:
    filing = Filing(
        cik="0000000005",
        company_name="Test Co",
        form=form,
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


def test_6k_truncation_does_not_alert_but_is_recorded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A 6-K exhibit larger than the 50k per-section cap, with an adverse term in
    the genuinely-unseen tail, must NOT raise a Discord alert (truncation is the
    6-K steady state — telemetry only, ADR 0033), but the exhibit_context event
    must still record the truncation against the 50k basis."""
    from filings_orchestrator.classify.classifier import _MAX_6K_SECTION_CHARS

    tail = " the company disclosed a going concern doubt."
    text = ("A" * (_MAX_6K_SECTION_CHARS + 5_000)) + tail  # adverse term past 50k
    doc = _doc(Exhibit(exhibit_type="EX-99.1", document="rpt.htm", url="u", text=text), form="6-K")

    with (
        patch(_CLASSIFY, return_value=_stub_classification(doc.filing.accession_number)),
        patch(_REDUCE, return_value=[]),
    ):
        classify_and_reduce(engine := _fresh_db(), doc)

    titles = [a.title for a in fetch_undelivered_alerts(engine)]
    assert "Adverse content truncated from exhibit" not in titles

    ctx = next(e for e in _events(capsys) if e["event"] == "exhibit_context")
    assert ctx["form"] == "6-K"
    assert ctx["truncated"] is True
    # dropped_chars measured against the 50k window, not the 16k context budget.
    assert ctx["dropped_chars"] == len(text) - _MAX_6K_SECTION_CHARS


def test_6k_fully_read_exhibit_is_not_truncated(capsys: pytest.CaptureFixture[str]) -> None:
    """A 6-K exhibit under the 50k cap is fully read, so it is NOT flagged
    truncated even if it contains adverse terms — the Robot-style false positive
    the 16k basis produced."""
    from filings_orchestrator.classify.classifier import _MAX_6K_SECTION_CHARS

    text = "Going concern is discussed here. " * 1_000  # ~33k chars, < 50k
    assert len(text) < _MAX_6K_SECTION_CHARS
    doc = _doc(Exhibit(exhibit_type="EX-99.1", document="pr.htm", url="u", text=text), form="6-K")

    with (
        patch(_CLASSIFY, return_value=_stub_classification(doc.filing.accession_number)),
        patch(_REDUCE, return_value=[]),
    ):
        classify_and_reduce(engine := _fresh_db(), doc)

    assert "Adverse content truncated from exhibit" not in [
        a.title for a in fetch_undelivered_alerts(engine)
    ]
    ctx = next(e for e in _events(capsys) if e["event"] == "exhibit_context")
    assert ctx["truncated"] is False
    assert ctx["dropped_chars"] == 0


def test_image_only_exhibit_emits_no_extractable_text_signal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An exhibit that extracts to ~no text (an image/scanned attachment) emits the
    measure-first `exhibit_no_extractable_text` signal so its unread content is
    visible. A text-bearing exhibit alongside it must not be flagged."""
    engine = _fresh_db()
    doc = _doc(
        Exhibit(exhibit_type="EX-99.1", document="ex1.htm", url="u", text="A real release. " * 30),
        Exhibit(exhibit_type="EX-99.2", document="ex2.htm", url="u", text="  "),  # image-only
    )

    with (
        patch(_CLASSIFY, return_value=_stub_classification(doc.filing.accession_number)),
        patch(_REDUCE, return_value=[]),
    ):
        classify_and_reduce(engine, doc)

    signal = next(e for e in _events(capsys) if e["event"] == "exhibit_no_extractable_text")
    assert signal["exhibits"] == ["EX-99.2"]
    assert signal["count"] == 1


def test_no_extractable_text_signal_absent_when_all_exhibits_have_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = _fresh_db()
    doc = _doc(Exhibit(exhibit_type="EX-99.1", document="ex1.htm", url="u", text="Release. " * 40))

    with (
        patch(_CLASSIFY, return_value=_stub_classification(doc.filing.accession_number)),
        patch(_REDUCE, return_value=[]),
    ):
        classify_and_reduce(engine, doc)

    assert "exhibit_no_extractable_text" not in [e["event"] for e in _events(capsys)]


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
