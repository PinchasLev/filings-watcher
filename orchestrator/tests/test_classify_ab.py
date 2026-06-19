"""Tests for classify-ab's baseline-leaf reconstruction (ADR 0032).

The diff/aggregate is covered in test_eval; the classify-both orchestration in
main() is thin glue over that and the classifier. What is specific here is
reconstructing a prior version's choice-set from its snapshot.
"""

from __future__ import annotations

from pathlib import Path

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
