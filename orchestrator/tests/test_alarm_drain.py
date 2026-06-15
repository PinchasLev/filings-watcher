"""Tests for the alarm-drain delivery worker (ADR 0031).

Exercises one drain pass over a seeded outbox with a fake Notifier (no network):
severity->channel routing, the freshness window, dedup coalescing, retry on a
failed POST, and the dry-run no-write guarantee. Uses in-memory SQLite with the
on-disk migrations applied, like test_alerts/test_persistence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, text

from filings_orchestrator.alerting import ALERT, INFO
from filings_orchestrator.alerting.drain import _drain
from filings_orchestrator.alerting.notify import Notification
from filings_orchestrator.alerting.outbox import insert_alert
from filings_orchestrator.persistence import apply_migrations, open_engine

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()

ALERTS_URL = "https://discord.test/alerts"
INFO_URL = "https://discord.test/info"
CHANNELS = {ALERT: ALERTS_URL, INFO: INFO_URL}


class FakeNotifier:
    """Records sends; can be told to fail every send to exercise retry."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, Notification]] = []
        self._fail = fail

    def send(self, webhook_url: str, notification: Notification) -> None:
        if self._fail:
            raise RuntimeError("discord unreachable")
        self.sent.append((webhook_url, notification))


def _fresh_db() -> Engine:
    engine = open_engine(":memory:")
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    return engine


def _seed(
    engine: Engine,
    *,
    severity: str,
    title: str,
    dedup_key: str | None = None,
    age_minutes: float = 0.0,
) -> None:
    created_at = (datetime.now(UTC) - timedelta(minutes=age_minutes)).isoformat()
    with engine.begin() as conn:
        insert_alert(
            conn, severity=severity, title=title, dedup_key=dedup_key, created_at=created_at
        )


def _undelivered_titles(engine: Engine) -> list[str]:
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT title FROM alerts_outbox WHERE delivered_at IS NULL ORDER BY id")
        ).fetchall()
    return [r[0] for r in rows]


def _drain_once(
    engine: Engine,
    notifier: FakeNotifier,
    *,
    info_ttl_minutes: float = 30.0,
    repeat_hours: float = 4.0,
    dry_run: bool = False,
) -> dict[str, int]:
    return _drain(
        engine,
        notifier,
        CHANNELS,
        limit=None,
        info_ttl_minutes=info_ttl_minutes,
        repeat_hours=repeat_hours,
        dry_run=dry_run,
    )


def test_routes_by_severity_and_marks_delivered() -> None:
    engine = _fresh_db()
    _seed(engine, severity=ALERT, title="panic")
    _seed(engine, severity=INFO, title="healed")
    notifier = FakeNotifier()

    counts = _drain_once(engine, notifier)

    assert counts["delivered"] == 2
    routed = {n.title: url for url, n in notifier.sent}
    assert routed == {"panic": ALERTS_URL, "healed": INFO_URL}
    # Both rows leave the work set.
    assert _undelivered_titles(engine) == []


def test_stale_info_is_suppressed_not_posted() -> None:
    engine = _fresh_db()
    _seed(engine, severity=INFO, title="old news", age_minutes=45)
    notifier = FakeNotifier()

    counts = _drain_once(engine, notifier, info_ttl_minutes=30.0)

    assert counts["suppressed_stale"] == 1
    assert counts["delivered"] == 0
    assert notifier.sent == []  # never POSTed
    assert _undelivered_titles(engine) == []  # but retired from the work set


def test_old_alert_is_not_subject_to_ttl() -> None:
    engine = _fresh_db()
    _seed(engine, severity=ALERT, title="old dead-letter", age_minutes=10_000)
    notifier = FakeNotifier()

    counts = _drain_once(engine, notifier, info_ttl_minutes=30.0)

    assert counts["delivered"] == 1
    assert counts["suppressed_stale"] == 0
    assert notifier.sent[0][1].title == "old dead-letter"


def test_dedup_suppresses_against_already_delivered_sibling() -> None:
    engine = _fresh_db()
    # First pass delivers the condition.
    _seed(engine, severity=ALERT, title="abandoned A", dedup_key="classification_abandoned:A")
    notifier = FakeNotifier()
    _drain_once(engine, notifier)
    assert len(notifier.sent) == 1

    # A later identical emit for the same condition must not re-page.
    _seed(engine, severity=ALERT, title="abandoned A again", dedup_key="classification_abandoned:A")
    notifier2 = FakeNotifier()
    counts = _drain_once(engine, notifier2)

    assert counts["suppressed_dup"] == 1
    assert notifier2.sent == []
    assert _undelivered_titles(engine) == []


def test_dedup_repages_after_repeat_window_lapses() -> None:
    engine = _fresh_db()
    _seed(engine, severity=ALERT, title="still broken", dedup_key="classify_outage")
    _drain_once(engine, FakeNotifier())  # first page

    # Backdate the delivered row beyond the repeat window: the condition is
    # still firing (a fresh row), so it must page again as a "still broken"
    # reminder rather than stay suppressed forever.
    with engine.begin() as conn:
        old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        conn.execute(
            text("UPDATE alerts_outbox SET delivered_at = :old WHERE delivered_at IS NOT NULL"),
            {"old": old},
        )
    _seed(engine, severity=ALERT, title="still broken (reminder)", dedup_key="classify_outage")
    notifier = FakeNotifier()
    counts = _drain_once(engine, notifier, repeat_hours=4.0)

    assert counts["delivered"] == 1
    assert counts["suppressed_dup"] == 0
    assert [n.title for _, n in notifier.sent] == ["still broken (reminder)"]


def test_dedup_coalesces_within_a_single_pass() -> None:
    engine = _fresh_db()
    _seed(engine, severity=ALERT, title="first", dedup_key="k")
    _seed(engine, severity=ALERT, title="second", dedup_key="k")
    notifier = FakeNotifier()

    counts = _drain_once(engine, notifier)

    assert counts["delivered"] == 1
    assert counts["suppressed_dup"] == 1
    assert [n.title for _, n in notifier.sent] == ["first"]  # oldest wins


def test_failed_post_keeps_row_for_retry_and_records_error() -> None:
    engine = _fresh_db()
    _seed(engine, severity=ALERT, title="will fail")
    notifier = FakeNotifier(fail=True)

    counts = _drain_once(engine, notifier)

    assert counts["failed"] == 1
    assert counts["delivered"] == 0
    # Row stays undelivered for the next pass; attempts/last_error recorded.
    with engine.begin() as conn:
        attempts, last_error, delivered_at = conn.execute(
            text("SELECT attempts, last_error, delivered_at FROM alerts_outbox")
        ).fetchone()
    assert attempts == 1
    assert "RuntimeError" in last_error
    assert delivered_at is None


def test_dry_run_writes_nothing() -> None:
    engine = _fresh_db()
    _seed(engine, severity=ALERT, title="panic")
    _seed(engine, severity=INFO, title="stale", age_minutes=999)
    notifier = FakeNotifier()

    counts = _drain_once(engine, notifier, dry_run=True)

    # Reports the intended outcomes but POSTs nothing and writes nothing.
    assert counts["delivered"] == 1
    assert counts["suppressed_stale"] == 1
    assert notifier.sent == []
    assert _undelivered_titles(engine) == ["panic", "stale"]
