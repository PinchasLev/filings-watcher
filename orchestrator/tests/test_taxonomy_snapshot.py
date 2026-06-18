"""Tests for the taxonomy snapshot subsystem (ADR 0032).

Covers the content hash (determinism, order-independence), cutting a version,
idempotent verification, and the integrity guards that make a forgotten version
bump or a tampered/append-to snapshot a loud abort rather than silent drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from filings_orchestrator.classify.taxonomy import (
    TAXONOMY_VERSION,
    hash_taxonomy_content,
    taxonomy_content_hash,
)
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.taxonomy_snapshot import (
    TaxonomyIntegrityError,
    _parse_major_minor,
    ensure_taxonomy_snapshot,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()


def _fresh_db() -> Engine:
    engine = open_engine(":memory:")
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    return engine


# --- content hash ---


def test_hash_is_order_independent() -> None:
    a = hash_taxonomy_content(
        [("financial", None), ("legal", None)],
        [("x", "desc x", "financial"), ("y", "desc y", "legal")],
    )
    b = hash_taxonomy_content(
        [("legal", None), ("financial", None)],
        [("y", "desc y", "legal"), ("x", "desc x", "financial")],
    )
    assert a == b


def test_hash_changes_with_content() -> None:
    base = hash_taxonomy_content([("financial", None)], [("x", "desc", "financial")])
    changed_desc = hash_taxonomy_content([("financial", None)], [("x", "DIFFERENT", "financial")])
    added_leaf = hash_taxonomy_content(
        [("financial", None)], [("x", "desc", "financial"), ("z", "new", "financial")]
    )
    assert base != changed_desc
    assert base != added_leaf


def test_parse_major_minor() -> None:
    assert _parse_major_minor("v1") == (1, 0)
    assert _parse_major_minor("v1.2") == (1, 2)
    assert _parse_major_minor("v2") == (2, 0)


# --- cut + verify ---


def test_ensure_cuts_current_version() -> None:
    engine = _fresh_db()
    ensure_taxonomy_snapshot(engine)

    with engine.begin() as conn:
        anchor = conn.execute(
            text(
                "SELECT major, minor, content_hash FROM taxonomy_versions "
                "WHERE taxonomy_version = :v"
            ),
            {"v": TAXONOMY_VERSION},
        ).fetchone()
        leaves = conn.execute(text("SELECT COUNT(*) FROM taxonomy_leaves")).scalar()
        domains = conn.execute(text("SELECT COUNT(*) FROM taxonomy_domains")).scalar()
    assert anchor is not None
    assert anchor[2] == taxonomy_content_hash()  # recorded hash matches the in-code hash
    assert leaves > 0
    assert domains > 0


def test_ensure_is_idempotent() -> None:
    engine = _fresh_db()
    ensure_taxonomy_snapshot(engine)
    ensure_taxonomy_snapshot(engine)  # second call verifies, must not raise or duplicate

    with engine.begin() as conn:
        versions = conn.execute(text("SELECT COUNT(*) FROM taxonomy_versions")).scalar()
    assert versions == 1


def test_ensure_aborts_on_code_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _fresh_db()
    ensure_taxonomy_snapshot(engine)  # cut with the real hash

    # Simulate editing taxonomy.py without bumping TAXONOMY_VERSION: the in-code
    # hash now differs from what the version was cut as.
    monkeypatch.setattr(
        "filings_orchestrator.persistence.taxonomy_snapshot.taxonomy_content_hash",
        lambda: "deadbeefdeadbeef",
    )
    with pytest.raises(TaxonomyIntegrityError, match="without a version bump"):
        ensure_taxonomy_snapshot(engine)


def test_ensure_aborts_on_snapshot_tamper_via_append() -> None:
    engine = _fresh_db()
    ensure_taxonomy_snapshot(engine)  # cut

    # Append a leaf to the already-cut version. Triggers block UPDATE/DELETE but
    # not INSERT, so this is allowed at the DB level — and the content hash is
    # exactly what catches it: the stored rows no longer hash to the anchor.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO taxonomy_leaves (taxonomy_version, leaf, description, domain) "
                "VALUES (:v, 'rogue_leaf', 'snuck in', 'operational')"
            ),
            {"v": TAXONOMY_VERSION},
        )
    with pytest.raises(TaxonomyIntegrityError, match="frozen snapshot rows were modified"):
        ensure_taxonomy_snapshot(engine)


# --- append-only triggers ---


@pytest.mark.parametrize("table", ["taxonomy_versions", "taxonomy_domains", "taxonomy_leaves"])
def test_update_and_delete_are_blocked(table: str) -> None:
    engine = _fresh_db()
    ensure_taxonomy_snapshot(engine)

    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as conn:
            conn.execute(text(f"UPDATE {table} SET taxonomy_version = 'vX'"))
    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {table}"))
