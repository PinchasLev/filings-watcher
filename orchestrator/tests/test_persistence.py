"""Tests for the persistence layer: schema, migrations, repository.

Uses in-memory SQLite (no temp files, no cleanup). Each test gets a fresh
engine and applies the on-disk migrations to it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from filings_orchestrator.classify import (
    Classification,
    EventDomain,
    EventType,
    FilingClassification,
    ItemClassification,
)
from filings_orchestrator.edgar.document import FilingDocument, ItemSection
from filings_orchestrator.edgar.models import Filing, FilingItem
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    advance_ingest_cursor,
    insert_classifications,
    latest_classifications_for_filing,
    read_ingest_cursor,
    upsert_filing,
    upsert_filing_document,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()


def _fresh_db() -> Engine:
    engine = open_engine(":memory:")
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    return engine


def _filing(accession: str = "0000320193-26-000045") -> Filing:
    return Filing(
        cik="0000320193",
        company_name="Apple Inc.",
        ticker="AAPL",
        form="8-K",
        accession_number=accession,
        filing_date=date(2026, 4, 30),
        report_date=date(2026, 4, 30),
        primary_document="aapl-20260430.htm",
        primary_document_url=(
            "https://www.sec.gov/Archives/edgar/data/320193/000032019326000045/aapl-20260430.htm"
        ),
        items=[FilingItem(number="2.02"), FilingItem(number="9.01")],
    )


def _filing_document() -> FilingDocument:
    return FilingDocument(
        filing=_filing(),
        text="Body text.",
        items=[ItemSection(number="2.02", title="Results", text="Press release.")],
        raw_size_bytes=12345,
    )


def _classification_result(
    classifier_version: str = "haiku-4.5+prompt-abcdef12",
    taxonomy_version: str = "v1",
) -> FilingClassification:
    return FilingClassification(
        accession_number="0000320193-26-000045",
        cik="0000320193",
        company_name="Apple Inc.",
        filing_date="2026-04-30",
        items=[
            ItemClassification(
                item_number="2.02",
                item_title="Results of Operations",
                classification=Classification(
                    event_type=EventType.EARNINGS_RELEASE,
                    is_material=True,
                    confidence=0.95,
                    reasoning="Quarterly earnings.",
                ),
            ),
        ],
        whole_filing=None,
        classified_at=datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC),
        model="haiku-4.5",
        classifier_version=classifier_version,
        taxonomy_version=taxonomy_version,
    )


def test_open_engine_enables_wal_journal_mode(tmp_path: Path) -> None:
    """WAL must be enabled on file-backed databases — required for concurrent
    reads from the Go service alongside the Python writer."""
    db_path = tmp_path / "wal.db"
    engine = open_engine(str(db_path))
    with engine.begin() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar_one()
    assert str(mode).lower() == "wal"


def test_default_migrations_dir_resolves_to_real_directory() -> None:
    """The default migrations dir resolved from this package must point at the
    real on-disk db/migrations directory. Regression guard for the off-by-one
    in `_MIGRATIONS_DIR_RELATIVE` that briefly resolved to the repo root."""
    from filings_orchestrator.persistence.migrations import _migrations_dir

    resolved = _migrations_dir()
    assert resolved.exists(), f"default migrations dir does not exist: {resolved}"
    assert (resolved / "001_initial_schema.sql").exists()


def test_split_statements_keeps_trigger_body_intact() -> None:
    """A CREATE TRIGGER with `;`-terminated body statements is one statement.

    Guards the migration runner's BEGIN/END-aware splitter — a naive split(';')
    would tear the trigger into fragments.
    """
    from filings_orchestrator.persistence.migrations import _split_statements

    sql = (
        "CREATE TABLE t (a INTEGER);\n"
        "CREATE TRIGGER t_no_update BEFORE UPDATE ON t\n"
        "BEGIN\n"
        "    SELECT RAISE(ABORT, 'append-only');\n"
        "END;\n"
        "CREATE INDEX t_idx ON t (a);"
    )
    statements = _split_statements(sql)
    assert len(statements) == 3
    assert statements[0].startswith("CREATE TABLE")
    assert statements[1].startswith("CREATE TRIGGER")
    assert "RAISE(ABORT, 'append-only')" in statements[1]
    assert statements[1].rstrip().endswith("END")
    assert statements[2].startswith("CREATE INDEX")


def test_apply_migrations_creates_tables_and_records_version() -> None:
    engine = open_engine(":memory:")
    applied = apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    assert [m.version for m in applied] == [
        "001_initial_schema",
        "002_ingest_cursor",
        "003_cik_tickers",
        "004_runs_and_events",
        "005_llm_calls",
        "006_submitted_at",
        "007_classify_attempts",
        "008_alerts_outbox",
        "009_exhibits",
        "010_taxonomy_snapshots",
        "011_classifications_append_only",
        "012_insider_transactions",
        "013_insider_filings_and_cursor",
        "014_insider_derivative_transactions",
        "015_periodic_filings",
        "016_filing_block_embeddings",
    ]

    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))
        }
    assert {
        "filings",
        "classifications",
        "ingest_cursor",
        "cik_tickers",
        "runs",
        "events",
        "event_classifications",
        "llm_calls",
        "alerts_outbox",
        "taxonomy_versions",
        "taxonomy_domains",
        "taxonomy_leaves",
        "schema_versions",
        "insider_transactions",
        "insider_filings",
        "form4_ingest_cursor",
        "insider_derivative_transactions",
        "periodic_filings",
        "filing_blocks",
        "periodic_ingest_cursor",
        "filing_block_embeddings",
    }.issubset(tables)


def test_apply_migrations_is_idempotent() -> None:
    """Running migrations twice on the same DB is a no-op the second time."""
    engine = open_engine(":memory:")
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    second_run = apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    assert second_run == []


def test_upsert_filing_inserts_then_updates() -> None:
    engine = _fresh_db()
    upsert_filing(engine, _filing())

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT company_name FROM filings WHERE accession_number = :a"),
            {"a": "0000320193-26-000045"},
        ).fetchone()
    assert row is not None
    assert row[0] == "Apple Inc."

    # Same accession, different company name → upsert wins.
    updated = _filing()
    updated_dict = updated.model_dump()
    updated_dict["company_name"] = "Apple Inc. (renamed)"
    upsert_filing(engine, Filing(**updated_dict))

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT company_name FROM filings WHERE accession_number = :a"),
            {"a": "0000320193-26-000045"},
        ).fetchone()
    assert row is not None
    assert row[0] == "Apple Inc. (renamed)"


def test_upsert_filing_persists_submitted_at_and_preserves_it_on_null_re_ingest() -> None:
    """Bitemporal correctness: a non-null `submitted_at` (atom-path-set) must
    not be wiped by a subsequent re-ingest whose `submitted_at` is None
    (e.g., a daily-index re-fetch of the same accession). The COALESCE in
    upsert_filing protects the earlier, more precise value."""
    engine = _fresh_db()

    # First write: atom-path-style, submitted_at populated.
    atom_filing_dict = _filing().model_dump()
    atom_filing_dict["submitted_at"] = "2026-06-05T14:35:39-04:00"
    upsert_filing(engine, Filing(**atom_filing_dict))

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT submitted_at FROM filings WHERE accession_number = :a"),
            {"a": "0000320193-26-000045"},
        ).fetchone()
    assert row is not None
    assert row[0] == "2026-06-05T14:35:39-04:00"

    # Second write: daily-index-style, submitted_at None. COALESCE preserves.
    daily_filing = _filing()  # default submitted_at is None
    upsert_filing(engine, daily_filing)

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT submitted_at FROM filings WHERE accession_number = :a"),
            {"a": "0000320193-26-000045"},
        ).fetchone()
    assert row is not None
    assert row[0] == "2026-06-05T14:35:39-04:00"


def test_upsert_filing_document_writes_body_and_sections() -> None:
    engine = _fresh_db()
    upsert_filing_document(engine, _filing_document())

    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT body_text, body_size_bytes, sections_json "
                "FROM filings WHERE accession_number = :a"
            ),
            {"a": "0000320193-26-000045"},
        ).fetchone()
    assert row is not None
    body_text, body_size, sections_json = row
    assert body_text == "Body text."
    assert body_size == 12345
    assert "2.02" in sections_json
    assert "Results" in sections_json


def test_insert_classifications_persists_rows_with_domain_denormalized() -> None:
    engine = _fresh_db()
    upsert_filing(engine, _filing())
    inserted = insert_classifications(engine, _classification_result())
    assert inserted == 1

    rows = latest_classifications_for_filing(engine, "0000320193-26-000045")
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "earnings_release"
    assert row["event_domain"] == EventDomain.FINANCIAL.value
    assert row["confidence"] == 0.95
    assert row["is_material"] == 1
    assert row["classifier_version"] == "haiku-4.5+prompt-abcdef12"
    assert row["taxonomy_version"] == "v1"


def test_insert_classifications_same_version_is_idempotent() -> None:
    """Re-running the same classifier version on the same filing must not
    duplicate rows. The UNIQUE INDEX rejects, INSERT OR IGNORE swallows."""
    engine = _fresh_db()
    upsert_filing(engine, _filing())
    result = _classification_result()

    first = insert_classifications(engine, result)
    second = insert_classifications(engine, result)

    assert first == 1
    assert second == 0


def test_classifications_update_and_delete_are_blocked() -> None:
    """Classifications are append-only (ADR 0011/0032): UPDATE and DELETE abort,
    while the normal INSERT path is unaffected."""
    engine = _fresh_db()
    upsert_filing(engine, _filing())
    assert insert_classifications(engine, _classification_result()) == 1  # INSERT still works

    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE classifications SET confidence = 0.1"))
    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM classifications"))

    # The row is untouched by the rejected mutations.
    rows = latest_classifications_for_filing(engine, "0000320193-26-000045")
    assert len(rows) == 1
    assert rows[0]["confidence"] == 0.95

    rows = latest_classifications_for_filing(engine, "0000320193-26-000045")
    assert len(rows) == 1


def test_insert_classifications_different_versions_coexist() -> None:
    """New classifier_version produces a new row alongside the old one.
    This is the foundation of ADR 0011's append-only model and A/B testing."""
    engine = _fresh_db()
    upsert_filing(engine, _filing())

    insert_classifications(engine, _classification_result(classifier_version="v1"))
    insert_classifications(engine, _classification_result(classifier_version="v2"))

    rows = latest_classifications_for_filing(engine, "0000320193-26-000045")
    versions = {r["classifier_version"] for r in rows}
    assert versions == {"v1", "v2"}


def test_insert_classifications_whole_filing_row_when_no_items() -> None:
    """When the classifier produces only a whole-filing classification
    (no item sections), one row is written with item_number IS NULL."""
    engine = _fresh_db()
    upsert_filing(engine, _filing())

    result = _classification_result()
    result_dict = result.model_dump()
    result_dict["items"] = []
    result_dict["whole_filing"] = Classification(
        event_type=EventType.OTHER_MATERIAL,
        is_material=True,
        confidence=0.7,
        reasoning="Whole filing.",
    ).model_dump()
    result_with_whole = FilingClassification(**result_dict)

    inserted = insert_classifications(engine, result_with_whole)
    assert inserted == 1

    rows = latest_classifications_for_filing(engine, "0000320193-26-000045")
    assert len(rows) == 1
    assert rows[0]["item_number"] is None
    assert rows[0]["event_type"] == "other_material"


def test_whole_filing_unique_constraint_with_coalesce() -> None:
    """The UNIQUE INDEX must reject duplicate whole-filing classifications
    of the same classifier_version. NULL item_number is COALESCEd to ''."""
    engine = _fresh_db()
    upsert_filing(engine, _filing())

    result = _classification_result()
    result_dict = result.model_dump()
    result_dict["items"] = []
    result_dict["whole_filing"] = Classification(
        event_type=EventType.OTHER_MATERIAL,
        is_material=True,
        confidence=0.7,
        reasoning="x",
    ).model_dump()
    whole_only = FilingClassification(**result_dict)

    first = insert_classifications(engine, whole_only)
    second = insert_classifications(engine, whole_only)

    assert first == 1
    assert second == 0


def test_open_engine_creates_parent_directory(tmp_path: Path) -> None:
    """A path with a non-existent parent directory should be created automatically."""
    target = tmp_path / "nested" / "subdir" / "filings.db"
    assert not target.parent.exists()
    open_engine(str(target))
    assert target.parent.exists()


def test_apply_migrations_then_repository_round_trip(tmp_path: Path) -> None:
    """End-to-end: open DB on disk, migrate, write filing + classification,
    read back, ensure data round-trips."""
    db_path = tmp_path / "round_trip.db"
    engine = open_engine(str(db_path))
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)

    upsert_filing(engine, _filing())
    insert_classifications(engine, _classification_result())

    rows = latest_classifications_for_filing(engine, "0000320193-26-000045")
    assert len(rows) == 1
    assert rows[0]["reasoning"] == "Quarterly earnings."


def test_ingest_cursor_singleton_constraint_rejects_second_id() -> None:
    """The ingest_cursor table holds exactly one row (id = 1). Any attempt
    to insert a different id is rejected by the CHECK constraint."""
    from sqlalchemy.exc import IntegrityError

    engine = _fresh_db()

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ingest_cursor (id, last_accession_number, last_filed_at, updated_at) "
                "VALUES (1, :a, :f, :u)"
            ),
            {"a": "0000320193-26-000045", "f": "2026-04-30", "u": "2026-04-30T12:00:00+00:00"},
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ingest_cursor "
                    "(id, last_accession_number, last_filed_at, updated_at) "
                    "VALUES (2, :a, :f, :u)"
                ),
                {"a": "0000320193-26-000046", "f": "2026-05-01", "u": "2026-05-01T12:00:00+00:00"},
            )


def test_read_ingest_cursor_returns_none_when_empty() -> None:
    """First-ever tick: no cursor yet."""
    engine = _fresh_db()
    assert read_ingest_cursor(engine) is None


def test_advance_ingest_cursor_inserts_then_upserts() -> None:
    engine = _fresh_db()
    advance_ingest_cursor(engine, "0001171843-26-003455", "20260515")
    assert read_ingest_cursor(engine) == ("0001171843-26-003455", "20260515")

    advance_ingest_cursor(engine, "0001193125-26-225361", "20260516")
    assert read_ingest_cursor(engine) == ("0001193125-26-225361", "20260516")


def test_ingest_cursor_upsert_overwrites_singleton_row() -> None:
    """Subsequent advances reuse id = 1 via ON CONFLICT DO UPDATE — the
    cursor never grows beyond one row."""
    engine = _fresh_db()

    upsert_sql = text(
        "INSERT INTO ingest_cursor (id, last_accession_number, last_filed_at, updated_at) "
        "VALUES (1, :a, :f, :u) "
        "ON CONFLICT (id) DO UPDATE SET "
        "  last_accession_number = excluded.last_accession_number, "
        "  last_filed_at         = excluded.last_filed_at, "
        "  updated_at            = excluded.updated_at"
    )

    with engine.begin() as conn:
        conn.execute(
            upsert_sql,
            {"a": "0000000000-26-000001", "f": "2026-05-19", "u": "2026-05-19T10:00:00+00:00"},
        )
        conn.execute(
            upsert_sql,
            {"a": "0000000000-26-000002", "f": "2026-05-19", "u": "2026-05-19T10:15:00+00:00"},
        )
        rows = conn.execute(text("SELECT id, last_accession_number FROM ingest_cursor")).fetchall()

    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1] == "0000000000-26-000002"


@pytest.mark.parametrize(
    "method_name",
    ["upsert_filing", "insert_classifications"],
)
def test_repository_requires_migrations_applied(method_name: str) -> None:
    """If migrations haven't run, repository calls should fail loudly rather
    than silently doing nothing."""
    from sqlalchemy.exc import OperationalError

    engine = open_engine(":memory:")
    # NOT applying migrations on purpose.

    with pytest.raises(OperationalError):
        if method_name == "upsert_filing":
            upsert_filing(engine, _filing())
        else:
            insert_classifications(engine, _classification_result())
