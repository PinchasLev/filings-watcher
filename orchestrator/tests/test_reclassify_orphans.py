"""Tests for the classify-layer reconciler (ADR 0030).

Covers the repository helpers (`list_orphaned_accessions`, `load_filing_document`)
and the `reclassify-orphans` CLI. The CLI's classify+reduce tail
(`classify_and_reduce`) is patched, so no Anthropic call is made; the tests
exercise the reconciler's orchestration — orphan selection, the dry-run gate,
cost-cap stop, and continue-on-failure — not the map/reduce internals, which are
covered elsewhere.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from anthropic import APITimeoutError
from sqlalchemy import Engine, text

from filings_orchestrator.classify import (
    Classification,
    EventType,
    FilingClassification,
    ItemClassification,
)
from filings_orchestrator.cli.reclassify_orphans import _MAX_CLASSIFY_ATTEMPTS, main
from filings_orchestrator.edgar.document import FilingDocument, ItemSection
from filings_orchestrator.edgar.models import Filing, FilingItem
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    increment_classify_attempt,
    insert_classifications,
    list_orphaned_accessions,
    load_filing_document,
    upsert_filing_document,
)

_EMIT_PATCH = "filings_orchestrator.cli.reclassify_orphans.emit"

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


def test_list_orphaned_accessions_grace_excludes_recently_fetched() -> None:
    engine = _fresh_db()
    upsert_filing_document(engine, _document(ORPHAN))  # fetched_at defaults to now
    now = datetime.now(UTC)
    cutoff = (now - timedelta(minutes=5)).isoformat()

    # Just-fetched: the live path is probably still classifying it, so a 5-minute
    # grace window excludes it — the reconciler must not race the live tick.
    assert list_orphaned_accessions(engine, fetched_before=cutoff) == []

    # Backdate the row past the window → a genuine orphan the live path is done
    # with, so it is now in the work set.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE filings SET fetched_at = :old WHERE accession_number = :acc"),
            {"old": (now - timedelta(minutes=10)).isoformat(), "acc": ORPHAN},
        )
    assert list_orphaned_accessions(engine, fetched_before=cutoff) == [ORPHAN]

    # No cutoff → unconditional orphan (the default, unchanged behavior).
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
    # These CLI tests upsert orphans with fetched_at = now and exercise the
    # heal/abandon logic, not the grace window. Disable the window so a
    # just-seeded orphan is in the work set; the window itself is covered by the
    # repository test and the dedicated CLI test below.
    monkeypatch.setenv("ORPHAN_GRACE_MINUTES", "0")
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


def test_grace_window_skips_a_just_fetched_orphan(
    configured_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Override the fixture's disabled window with a real one: a just-upserted
    # orphan (fetched_at = now) is within the grace window, so the reconciler
    # must leave it for the live tick rather than race it.
    monkeypatch.setenv("ORPHAN_GRACE_MINUTES", "5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    upsert_filing_document(open_engine(str(configured_db)), _document(ORPHAN))
    monkeypatch.setattr("sys.argv", ["reclassify-orphans"])

    with patch(_CLASSIFY_PATCH) as mock_cr:
        main()
    mock_cr.assert_not_called()  # within grace → not healed
    # Still an orphan (no cutoff), just deliberately deferred this run.
    assert list_orphaned_accessions(open_engine(str(configured_db))) == [ORPHAN]


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


# --- dead-letter (ADR 0030) ---


def test_list_orphaned_accessions_excludes_abandoned_at_limit() -> None:
    engine = _fresh_db()
    upsert_filing_document(engine, _document(ORPHAN))
    for _ in range(_MAX_CLASSIFY_ATTEMPTS):
        increment_classify_attempt(engine, ORPHAN)

    assert list_orphaned_accessions(engine) == [ORPHAN]  # unfiltered: still an orphan
    assert list_orphaned_accessions(engine, max_attempts=_MAX_CLASSIFY_ATTEMPTS) == []  # abandoned


def test_increment_classify_attempt_returns_new_count() -> None:
    engine = _fresh_db()
    upsert_filing_document(engine, _document(ORPHAN))
    assert increment_classify_attempt(engine, ORPHAN) == 1
    assert increment_classify_attempt(engine, ORPHAN) == 2
    assert increment_classify_attempt(engine, "9999999999-99-999999") == 0  # absent: no-op


def test_deterministic_failure_abandons_at_limit(
    configured_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = open_engine(str(configured_db))
    upsert_filing_document(engine, _document(ORPHAN))
    for _ in range(_MAX_CLASSIFY_ATTEMPTS - 1):  # one short of the limit
        increment_classify_attempt(engine, ORPHAN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("sys.argv", ["reclassify-orphans"])

    def _boom(engine: Engine, document: FilingDocument) -> int:
        raise RuntimeError("schema rejected the model output")

    events: list[str] = []

    with (
        patch(_CLASSIFY_PATCH, side_effect=_boom),
        patch(_EMIT_PATCH, side_effect=lambda name, **_: events.append(name)),
        pytest.raises(SystemExit) as exc,
    ):
        main()
    assert exc.value.code == 1
    assert "classification_abandoned" in events

    fresh = open_engine(str(configured_db))
    assert list_orphaned_accessions(fresh, max_attempts=_MAX_CLASSIFY_ATTEMPTS) == []  # parked
    assert list_orphaned_accessions(fresh) == [ORPHAN]  # not lost


def test_transient_failure_does_not_count_toward_abandonment(
    configured_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_filing_document(open_engine(str(configured_db)), _document(ORPHAN))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("sys.argv", ["reclassify-orphans"])

    timeout = APITimeoutError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    with patch(_CLASSIFY_PATCH, side_effect=timeout), pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    # A transient outage must not park an otherwise-healthy filing.
    assert list_orphaned_accessions(
        open_engine(str(configured_db)), max_attempts=_MAX_CLASSIFY_ATTEMPTS
    ) == [ORPHAN]


def test_force_retries_the_abandoned_set(
    configured_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = open_engine(str(configured_db))
    upsert_filing_document(engine, _document(ORPHAN))
    for _ in range(_MAX_CLASSIFY_ATTEMPTS):  # already abandoned
        increment_classify_attempt(engine, ORPHAN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def _heal(engine: Engine, document: FilingDocument) -> int:
        insert_classifications(engine, _classification(document.filing.accession_number))
        return 0

    # Normal run skips the abandoned filing entirely.
    monkeypatch.setattr("sys.argv", ["reclassify-orphans"])
    with patch(_CLASSIFY_PATCH, side_effect=_heal) as mock_cr:
        main()
    mock_cr.assert_not_called()
    assert list_orphaned_accessions(open_engine(str(configured_db))) == [ORPHAN]

    # --force re-includes it and heals it.
    monkeypatch.setattr("sys.argv", ["reclassify-orphans", "--force"])
    with patch(_CLASSIFY_PATCH, side_effect=_heal) as mock_cr:
        main()
    mock_cr.assert_called_once()
    assert list_orphaned_accessions(open_engine(str(configured_db))) == []
