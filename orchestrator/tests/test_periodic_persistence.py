"""Tests for periodic-filing persistence (ADR 0042, PR 2).

Hermetic — a tmp SQLite DB with migrations applied. Exercises the envelope + blocks
writer, dedup, and the resumable cursor.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from filings_orchestrator.change_detection import RiskFactorBlock
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    advance_periodic_cursor,
    insert_periodic_filing,
    read_periodic_cursor,
    select_seen_periodic_accessions,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = open_engine(str(tmp_path / "filings.db"))
    apply_migrations(eng, migrations_dir=MIGRATIONS_DIR)
    return eng


def _block(i: int, text_: str) -> RiskFactorBlock:
    return RiskFactorBlock(index=i, heading=f"Heading {i}", text=text_, block_hash=f"hash{i}")


def _envelope(engine: Engine, accession: str) -> dict[str, object]:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT cik, form, period_of_report, fiscal_year, parsed, block_count "
                "FROM periodic_filings WHERE accession_number = :a"
            ),
            {"a": accession},
        ).one()
    return {
        "cik": row[0],
        "form": row[1],
        "period_of_report": row[2],
        "fiscal_year": row[3],
        "parsed": row[4],
        "block_count": row[5],
    }


def _block_rows(engine: Engine, accession: str) -> list[tuple[int, str]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT block_index, block_text FROM filing_blocks "
                "WHERE accession_number = :a ORDER BY block_index"
            ),
            {"a": accession},
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def test_envelope_and_blocks_round_trip(engine: Engine) -> None:
    insert_periodic_filing(
        engine,
        accession_number="0001-26-000010",
        cik="0000000123",
        company_name="ACME CORP",
        form="10-K",
        filed_at="2026-03-15",
        period_of_report="2025-12-31",
        fiscal_year=2025,
        parsed=True,
        blocks=[_block(0, "First risk."), _block(1, "Second risk.")],
        ingested_at="2026-03-15T12:00:00+00:00",
    )
    env = _envelope(engine, "0001-26-000010")
    assert env == {
        "cik": "0000000123",
        "form": "10-K",
        "period_of_report": "2025-12-31",
        "fiscal_year": 2025,
        "parsed": 1,
        "block_count": 2,
    }
    assert _block_rows(engine, "0001-26-000010") == [(0, "First risk."), (1, "Second risk.")]


def test_unparsed_filing_is_anchored_with_no_blocks(engine: Engine) -> None:
    insert_periodic_filing(
        engine,
        accession_number="0002-26-000020",
        cik="0000000123",
        company_name="ACME CORP",
        form="10-K",
        filed_at="2026-03-15",
        period_of_report=None,
        fiscal_year=None,
        parsed=False,
        blocks=[],
        ingested_at="2026-03-15T12:00:00+00:00",
    )
    env = _envelope(engine, "0002-26-000020")
    assert env["parsed"] == 0
    assert env["block_count"] == 0
    assert _block_rows(engine, "0002-26-000020") == []


def test_reingest_replaces_blocks_idempotently(engine: Engine) -> None:
    common: dict[str, object] = {
        "accession_number": "0003-26-000030",
        "cik": "0000000123",
        "company_name": "ACME CORP",
        "form": "10-K",
        "filed_at": "2026-03-15",
        "period_of_report": "2025-12-31",
        "fiscal_year": 2025,
        "parsed": True,
        "ingested_at": "2026-03-15T12:00:00+00:00",
    }
    insert_periodic_filing(
        engine, blocks=[_block(0, "a"), _block(1, "b"), _block(2, "c")], **common
    )
    insert_periodic_filing(engine, blocks=[_block(0, "only one now")], **common)
    assert _envelope(engine, "0003-26-000030")["block_count"] == 1
    assert _block_rows(engine, "0003-26-000030") == [(0, "only one now")]


def test_select_seen_and_cursor(engine: Engine) -> None:
    assert read_periodic_cursor(engine) is None
    assert select_seen_periodic_accessions(engine, ["0001-26-000010"]) == set()

    insert_periodic_filing(
        engine,
        accession_number="0001-26-000010",
        cik="0000000123",
        company_name="ACME CORP",
        form="10-K",
        filed_at="2026-03-15",
        period_of_report="2025-12-31",
        fiscal_year=2025,
        parsed=True,
        blocks=[_block(0, "risk")],
        ingested_at="2026-03-15T12:00:00+00:00",
    )
    assert select_seen_periodic_accessions(engine, ["0001-26-000010", "0009-26-000099"]) == {
        "0001-26-000010"
    }

    advance_periodic_cursor(engine, "0001-26-000010", "2026-03-15")
    assert read_periodic_cursor(engine) == ("0001-26-000010", "2026-03-15")
    advance_periodic_cursor(engine, "0004-26-000040", "2026-03-16")
    assert read_periodic_cursor(engine) == ("0004-26-000040", "2026-03-16")
