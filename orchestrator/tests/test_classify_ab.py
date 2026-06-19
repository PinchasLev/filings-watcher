"""Tests for classify-ab's baseline-leaf reconstruction (ADR 0032).

The diff/aggregate is covered in test_eval; the classify-both orchestration in
main() is thin glue over that and the classifier. What is specific here is
reconstructing a prior version's choice-set from its snapshot.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine

from filings_orchestrator.classify.taxonomy import TAXONOMY_VERSION, EventType
from filings_orchestrator.cli.classify_ab import _baseline_leaves
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.taxonomy_snapshot import (
    ensure_taxonomy_snapshot,
    leaves_for_version,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()


def _fresh_db() -> Engine:
    engine = open_engine(":memory:")
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    ensure_taxonomy_snapshot(engine)  # cut the current version
    return engine


def test_leaves_for_version_returns_the_snapshot_leaves() -> None:
    engine = _fresh_db()
    leaves = set(leaves_for_version(engine, TAXONOMY_VERSION))
    assert leaves == {e.value for e in EventType}  # current version == full in-code taxonomy


def test_baseline_leaves_are_eventtypes_in_declaration_order() -> None:
    engine = _fresh_db()
    baseline = _baseline_leaves(engine, TAXONOMY_VERSION)
    # Mapped back to EventType members, ordered like the enum (so the prompt order
    # matches that version's), and — for v1 today — the full set.
    assert baseline == list(EventType)


def test_baseline_leaves_empty_for_unknown_version() -> None:
    engine = _fresh_db()
    assert _baseline_leaves(engine, "v999") == []


def test_reuse_baseline_skips_reclassifying_the_baseline_arm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With --reuse-baseline, a filing whose stored classification matches the
    baseline arm's classifier_version is reused — only the candidate arm calls
    the LLM (one call, not two)."""
    import sys
    from datetime import UTC, date, datetime

    from filings_orchestrator.classify.classifier import DEFAULT_MODEL, classifier_version
    from filings_orchestrator.classify.schema import (
        Classification,
        FilingClassification,
        ItemClassification,
    )
    from filings_orchestrator.cli import classify_ab as mod
    from filings_orchestrator.edgar.document import FilingDocument
    from filings_orchestrator.edgar.models import Filing
    from filings_orchestrator.persistence.repository import (
        insert_classifications,
        upsert_filing_document,
    )

    db = tmp_path / "t.db"
    monkeypatch.setenv("FILINGS_DB_PATH", str(db))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    engine = open_engine(str(db))
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    ensure_taxonomy_snapshot(engine)  # cut current version

    acc = "0000000005-26-000001"
    filing = Filing(
        cik="5",
        company_name="Co",
        form="8-K",
        accession_number=acc,
        filing_date=date(2026, 6, 16),
        primary_document="f.htm",
        primary_document_url="u",
    )
    upsert_filing_document(engine, FilingDocument(filing=filing, text="body", raw_size_bytes=4))

    # Baseline arm = current version (all leaves); store a classification under
    # exactly that classifier_version so it qualifies for reuse.
    baseline_cv = classifier_version(DEFAULT_MODEL, list(EventType))

    def _fc(cv: str, et: EventType) -> FilingClassification:
        return FilingClassification(
            accession_number=acc,
            cik="5",
            company_name="Co",
            filing_date="2026-06-16",
            items=[
                ItemClassification(
                    item_number="1.01",
                    item_title=None,
                    classification=Classification(
                        event_type=et, is_material=True, confidence=0.9, reasoning="r"
                    ),
                )
            ],
            whole_filing=None,
            classified_at=datetime.now(UTC),
            model=DEFAULT_MODEL,
            classifier_version=cv,
            taxonomy_version=TAXONOMY_VERSION,
        )

    insert_classifications(engine, _fc(baseline_cv, EventType.MA_ACTIVITY))

    calls: list[object] = []

    def fake_classify(document: object, *a: object, **k: object) -> FilingClassification:
        calls.append(k.get("leaves"))
        return _fc("candidate", EventType.FINANCIAL_OTHER)

    monkeypatch.setattr(mod, "classify_filing", fake_classify)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "classify-ab",
            "--baseline-version",
            TAXONOMY_VERSION,
            "--accession",
            acc,
            "--reuse-baseline",
        ],
    )

    mod.main()

    # Baseline reused → only the candidate arm classified (one call, leaves=None).
    assert len(calls) == 1
    assert calls[0] is None
