"""Tests for the diff reconciler (ADR 0042, PR 4): pairing, readiness, storage.

Hermetic — a tmp SQLite DB with migrations applied. Filings are seeded with blocks
and hand-set embedding vectors so the resulting diff is deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from filings_orchestrator.change_detection import RiskFactorBlock
from filings_orchestrator.cli.diff_filings import diff_pass, main
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    UnembeddedBlock,
    insert_block_embeddings,
    insert_periodic_filing,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()
_MODEL = "test-model"
_SECTION = "risk_factors"
_CIK = "0000000123"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = open_engine(str(tmp_path / "filings.db"))
    apply_migrations(eng, migrations_dir=MIGRATIONS_DIR)
    return eng


def _seed_filing(
    engine: Engine,
    accession: str,
    period: str,
    vectors: list[list[float]],
    *,
    embed: bool = True,
) -> None:
    blocks = [
        RiskFactorBlock(
            index=i, heading=f"H{i}", text=f"{accession}-t{i}", block_hash=f"{accession}-{i}"
        )
        for i in range(len(vectors))
    ]
    insert_periodic_filing(
        engine,
        accession_number=accession,
        cik=_CIK,
        company_name="ACME CORP",
        form="10-K",
        filed_at="2026-01-01",
        period_of_report=period,
        fiscal_year=int(period[:4]),
        parsed=True,
        blocks=blocks,
        ingested_at="2026-01-01T00:00:00+00:00",
    )
    if embed:
        items = [
            (UnembeddedBlock(accession, _SECTION, i, f"{accession}-t{i}"), v)
            for i, v in enumerate(vectors)
        ]
        insert_block_embeddings(engine, model_id=_MODEL, items=items, embedded_at="t")


def _diff_row(engine: Engine, accession: str) -> dict[str, object] | None:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT prior_accession_number, added_count, changed_count, "
                "carried_count, dropped_count FROM filing_diffs WHERE accession_number = :a"
            ),
            {"a": accession},
        ).fetchone()
    if row is None:
        return None
    return {
        "prior": row[0],
        "added": row[1],
        "changed": row[2],
        "carried": row[3],
        "dropped": row[4],
    }


def _change_types(engine: Engine, accession: str) -> list[str]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT change_type FROM block_changes WHERE accession_number = :a "
                "ORDER BY change_seq"
            ),
            {"a": accession},
        ).fetchall()
    return [r[0] for r in rows]


def test_diffs_current_against_prior_and_stores_shortlist(engine: Engine) -> None:
    _seed_filing(engine, "prior", "2024-12-31", [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    _seed_filing(engine, "current", "2025-12-31", [[1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    # current b0 == prior b0 (carried); current b1 orthogonal (added); prior b1 dropped.

    counts = diff_pass(engine, section=_SECTION, model_id=_MODEL, limit=10)
    assert counts["computed"] == 1

    assert _diff_row(engine, "current") == {
        "prior": "prior",
        "added": 1,
        "changed": 0,
        "carried": 1,
        "dropped": 1,
    }
    assert sorted(_change_types(engine, "current")) == ["added", "dropped"]
    # The prior filing is the earliest -> never selected as a diff target.
    assert _diff_row(engine, "prior") is None


def test_first_filing_with_no_prior_is_not_diffed(engine: Engine) -> None:
    _seed_filing(engine, "only", "2025-12-31", [[1.0, 0.0, 0.0]])
    counts = diff_pass(engine, section=_SECTION, model_id=_MODEL, limit=10)
    assert counts == {"computed": 0, "skipped_prior_pending": 0, "candidates": 0}


def test_prior_not_embedded_is_skipped_and_retried(engine: Engine) -> None:
    _seed_filing(engine, "prior", "2024-12-31", [[1.0, 0.0, 0.0]], embed=False)
    _seed_filing(engine, "current", "2025-12-31", [[1.0, 0.0, 0.0]])

    counts = diff_pass(engine, section=_SECTION, model_id=_MODEL, limit=10)
    assert counts["computed"] == 0
    assert counts["skipped_prior_pending"] == 1
    assert _diff_row(engine, "current") is None

    # Once the prior is embedded, the same filing diffs on the next pass.
    items = [(UnembeddedBlock("prior", _SECTION, 0, "prior-t0"), [1.0, 0.0, 0.0])]
    insert_block_embeddings(engine, model_id=_MODEL, items=items, embedded_at="t")
    assert diff_pass(engine, section=_SECTION, model_id=_MODEL, limit=10)["computed"] == 1


def test_diff_is_idempotent(engine: Engine) -> None:
    _seed_filing(engine, "prior", "2024-12-31", [[1.0, 0.0, 0.0]])
    _seed_filing(engine, "current", "2025-12-31", [[0.0, 1.0, 0.0]])
    assert diff_pass(engine, section=_SECTION, model_id=_MODEL, limit=10)["computed"] == 1
    # Already diffed -> not selected again.
    assert diff_pass(engine, section=_SECTION, model_id=_MODEL, limit=10)["computed"] == 0


def test_main_runs_the_pass(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "filings.db"
    eng = open_engine(str(db_path))
    apply_migrations(eng, migrations_dir=MIGRATIONS_DIR)
    _seed_filing(eng, "prior", "2024-12-31", [[1.0, 0.0, 0.0]])
    _seed_filing(eng, "current", "2025-12-31", [[0.0, 1.0, 0.0]])

    monkeypatch.setenv("FILINGS_DB_PATH", str(db_path))
    monkeypatch.setenv("VOYAGE_MODEL", _MODEL)
    monkeypatch.setattr(sys, "argv", ["diff-filings"])
    main()

    assert _diff_row(eng, "current") is not None
