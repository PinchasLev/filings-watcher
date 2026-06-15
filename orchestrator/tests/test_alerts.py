"""Tests for the alerting outbox (ADR 0031): emit_alert + outbox DB half.

Uses in-memory SQLite with the on-disk migrations applied, like
test_persistence. Covers the producer side only — the drainer (delivery to
Discord) is exercised in test_alarm_drain.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from filings_orchestrator.alerting import ALERT, INFO, emit_alert
from filings_orchestrator.alerting.outbox import fetch_undelivered_alerts, insert_alert
from filings_orchestrator.persistence import apply_migrations, open_engine

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()


def _fresh_db() -> Engine:
    engine = open_engine(":memory:")
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    return engine


def test_emit_alert_enqueues_undelivered_row() -> None:
    engine = _fresh_db()
    emit_alert(
        engine,
        ALERT,
        "Classification abandoned",
        body="0000320193-26-000045 was dead-lettered.",
        dedup_key="classification_abandoned:0000320193-26-000045",
        accession="0000320193-26-000045",
        attempts=3,
    )

    pending = fetch_undelivered_alerts(engine)
    assert len(pending) == 1
    row = pending[0]
    assert row.severity == ALERT
    assert row.title == "Classification abandoned"
    assert row.body == "0000320193-26-000045 was dead-lettered."
    assert row.dedup_key == "classification_abandoned:0000320193-26-000045"
    # Keyword args land in the structured fields blob, JSON round-tripped.
    assert row.fields == {"accession": "0000320193-26-000045", "attempts": 3}


def test_emit_alert_defaults_body_and_dedup_to_null() -> None:
    engine = _fresh_db()
    emit_alert(engine, INFO, "Orphans reclassified", reclassified=4)

    row = fetch_undelivered_alerts(engine)[0]
    assert row.severity == INFO
    assert row.body is None
    assert row.dedup_key is None
    assert row.fields == {"reclassified": 4}


def test_emit_alert_rejects_unknown_severity() -> None:
    engine = _fresh_db()
    with pytest.raises(ValueError, match="unknown alert severity"):
        emit_alert(engine, "critical", "nope")
    # Nothing was written on the rejected emit.
    assert fetch_undelivered_alerts(engine) == []


def test_fetch_undelivered_orders_oldest_first_and_excludes_delivered() -> None:
    engine = _fresh_db()
    emit_alert(engine, INFO, "first", created_marker=1)
    emit_alert(engine, INFO, "second", created_marker=2)
    emit_alert(engine, ALERT, "third", created_marker=3)

    # Mark the middle one delivered; it should drop out of the work set.
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE alerts_outbox SET delivered_at = '2026-06-15T00:00:00+00:00' "
                "WHERE title = :t"
            ),
            {"t": "second"},
        )

    titles = [r.title for r in fetch_undelivered_alerts(engine)]
    assert titles == ["first", "third"]


def test_fetch_undelivered_respects_limit() -> None:
    engine = _fresh_db()
    for i in range(5):
        emit_alert(engine, INFO, f"alert-{i}")
    assert len(fetch_undelivered_alerts(engine, limit=2)) == 2


def test_insert_alert_enlists_in_caller_transaction() -> None:
    """The Connection-taking half commits atomically with the caller's work.

    A failure after insert_alert but before commit must roll the alert back —
    the transactional-outbox guarantee. Here the surrounding transaction raises,
    so no row should survive.
    """
    engine = _fresh_db()
    with pytest.raises(RuntimeError, match="boom"):
        with engine.begin() as conn:
            insert_alert(conn, severity=ALERT, title="should roll back")
            raise RuntimeError("boom")
    assert fetch_undelivered_alerts(engine) == []
