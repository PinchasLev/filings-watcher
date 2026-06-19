"""Tests for exhibit-ab's sample-selection query.

The diff/aggregate core moved to cli/_eval.py (see test_eval.py); the
classify-both-and-report orchestration in main() is thin glue over that core and
the already-tested classifier. What remains exhibit-specific here is the
population query.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import Engine

from filings_orchestrator.edgar.document import FilingDocument
from filings_orchestrator.edgar.models import Exhibit, Filing
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    list_exhibit_bearing_accessions,
    upsert_filing_document,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()


def _fresh_db() -> Engine:
    engine = open_engine(":memory:")
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    return engine


def _doc(accession: str, *, exhibits: list[Exhibit]) -> FilingDocument:
    filing = Filing(
        cik="0000000005",
        company_name="Test Co",
        form="8-K",
        accession_number=accession,
        filing_date=date(2026, 6, 16),
        primary_document="f.htm",
        primary_document_url="https://example.test/f.htm",
    )
    return FilingDocument(filing=filing, text="body", exhibits=exhibits, raw_size_bytes=4)


def test_list_exhibit_bearing_selects_only_filings_with_exhibits_and_body() -> None:
    engine = _fresh_db()
    ex = [Exhibit(exhibit_type="EX-99.1", document="ex1.htm", url="u", text="Release.")]
    upsert_filing_document(engine, _doc("0000000005-26-000001", exhibits=ex))
    upsert_filing_document(engine, _doc("0000000005-26-000002", exhibits=[]))  # no exhibits

    got = list_exhibit_bearing_accessions(engine)
    assert got == ["0000000005-26-000001"]


def test_list_exhibit_bearing_respects_limit() -> None:
    engine = _fresh_db()
    ex = [Exhibit(exhibit_type="EX-99.1", document="ex1.htm", url="u", text="x")]
    for i in range(3):
        upsert_filing_document(engine, _doc(f"0000000005-26-00000{i}", exhibits=ex))
    assert len(list_exhibit_bearing_accessions(engine, limit=2)) == 2
