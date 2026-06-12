"""Tests for the classify-layer reconciler (ADR 0030).

Covers the repository helpers (`list_orphaned_accessions`, `load_filing_document`)
and the `reclassify-orphans` CLI. The CLI's classify+reduce tail
(`classify_and_reduce`) is patched, so no Anthropic call is made; the tests
exercise the reconciler's orchestration — orphan selection, the dry-run gate,
cost-cap stop, and continue-on-failure — not the map/reduce internals, which are
covered elsewhere.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import Engine

from filings_orchestrator.classify import (
    Classification,
    EventType,
    FilingClassification,
    ItemClassification,
)
from filings_orchestrator.cli.reclassify_orphans import main
from filings_orchestrator.edgar.document import FilingDocument, ItemSection
from filings_orchestrator.edgar.models import Filing, FilingItem
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    insert_classifications,
    list_orphaned_accessions,
    load_filing_document,
    upsert_filing_document,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()

ORPHAN = "0001922446-26-000004"
CLASSIFIED = "0000320193-26-000099"

_CLASSIFY_PATCH = "filings_orchestrator.cli.reclassify_orphans.classify_and_reduce"


def _document(accession: str, items: tuple[str, ...] = ("1.01", "5.02")) -> FilingDocument:
    return FilingDocument(
        filing=Filing(
            cik="0001922446",
            company_name="Diversified Energy Co",
            ticker="DEC",
            form="8-K",
            accession_number=accession,
            filing_date=date(2026, 5, 21),
            report_date=None,
            primary_document="dec.htm",
            primary_document_url="https://www.sec.gov/Archives/edgar/data/1922446/x/dec.htm",
            items=[FilingItem(number=n) for n in items],
            submitted_at="2026-05-21T09:05:00-04:00",
        ),
        text="Body text of the filing.",
        items=[ItemSection(number=n, title=None, text=f"Item {n} prose.") for n in items],
        raw_size_bytes=1234,
    )


def _classification(accession: str) -> FilingClassification:
    return FilingClassification(
        accession_number=accession,
        cik="0001922446",
        company_name="Diversified Energy Co",
        filing_date="2026-05-21",
        items=[
            ItemClassification(
                item_number="1.01",
                item_title=None,
                classification=Classification(
                    event_type=EventType("ma_activity"),
                    is_material=True,
                    confidence=0.9,
                    reasoning="Item 1.01.",
                ),
            )
        ],
        whole_filing=None,
        classified_at=datetime(2026, 5, 22, tzinfo=UTC),
        model="haiku-4.5",
        classifier_version="haiku-4.5+prompt-aaaa1111",
        taxonomy_version="v1",
    )


def _fresh_db() -> Engine:
    engine = open_engine(":memory:")
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    return engine


# --- repository helpers ---


def test_list_orphaned_accessions_returns_only_unclassified() -> None:
    engine = _fresh_db()
    upsert_filing_document(engine, _document(ORPHAN))  # no classifications → orphan
    upsert_filing_document(engine, _document(CLASSIFIED))
    insert_classifications(engine, _classification(CLASSIFIED))

    assert list_orphaned_accessions(engine) == [ORPHAN]


def test_load_filing_document_reconstructs_from_stored_row() -> None:
    engine = _fresh_db()
    upsert_filing_document(engine, _document(ORPHAN))

    doc = load_filing_document(engine, ORPHAN)
    assert doc is not None
    assert doc.filing.accession_number == ORPHAN
    assert doc.filing.company_name == "Diversified Energy Co"
    assert doc.filing.submitted_at == "2026-05-21T09:05:00-04:00"
    assert doc.text == "Body text of the filing."
    assert [s.number for s in doc.items] == ["1.01", "5.02"]
    assert [i.number for i in doc.filing.items] == ["1.01", "5.02"]


def test_load_filing_document_returns_none_when_absent() -> None:
    engine = _fresh_db()
    assert load_filing_document(engine, "9999999999-99-999999") is None


# --- CLI ---


@pytest.fixture
def configured_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "filings.db"
    monkeypatch.setenv("FILINGS_DB_PATH", str(db_path))
    apply_migrations(open_engine(str(db_path)), migrations_dir=MIGRATIONS_DIR)
    return db_path


def test_dry_run_lists_orphans_and_exits_nonzero(
    configured_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_filing_document(open_engine(str(configured_db)), _document(ORPHAN))
    monkeypatch.setattr("sys.argv", ["reclassify-orphans", "--dry-run"])

    with patch(_CLASSIFY_PATCH) as mock_cr, pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    mock_cr.assert_not_called()  # dry-run never classifies


def test_dry_run_exits_zero_when_no_orphans(
    configured_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = open_engine(str(configured_db))
    upsert_filing_document(engine, _document(CLASSIFIED))
    insert_classifications(engine, _classification(CLASSIFIED))
    monkeypatch.setattr("sys.argv", ["reclassify-orphans", "--dry-run"])

    with patch(_CLASSIFY_PATCH):
        main()  # returns normally, no SystemExit


def test_heal_reclassifies_and_clears_orphan(
    configured_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_filing_document(open_engine(str(configured_db)), _document(ORPHAN))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("sys.argv", ["reclassify-orphans"])

    def _heal(engine: Engine, document: FilingDocument) -> int:
        insert_classifications(engine, _classification(document.filing.accession_number))
        return 0

    with patch(_CLASSIFY_PATCH, side_effect=_heal) as mock_cr:
        main()
    mock_cr.assert_called_once()
    assert mock_cr.call_args.args[1].filing.accession_number == ORPHAN
    assert list_orphaned_accessions(open_engine(str(configured_db))) == []


def test_heal_stops_at_cost_cap_before_classifying(
    configured_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_filing_document(open_engine(str(configured_db)), _document(ORPHAN))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_DAILY_COST_CAP_USD", "0.0")  # already at cap
    monkeypatch.setattr("sys.argv", ["reclassify-orphans"])

    with patch(_CLASSIFY_PATCH) as mock_cr:
        main()
    mock_cr.assert_not_called()
    assert list_orphaned_accessions(open_engine(str(configured_db))) == [ORPHAN]


def test_heal_continues_past_a_failure(
    configured_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = "0000000000-26-000001"
    good = "0000000000-26-000002"
    engine = open_engine(str(configured_db))
    upsert_filing_document(engine, _document(bad))
    upsert_filing_document(engine, _document(good))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("sys.argv", ["reclassify-orphans"])

    def _heal(engine: Engine, document: FilingDocument) -> int:
        if document.filing.accession_number == bad:
            raise RuntimeError("classify exploded")
        insert_classifications(engine, _classification(document.filing.accession_number))
        return 0

    with patch(_CLASSIFY_PATCH, side_effect=_heal), pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1  # a failure occurred

    # The good one healed; the bad one is still an orphan.
    assert list_orphaned_accessions(open_engine(str(configured_db))) == [bad]
