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


# --- Episodes: one message tells a one-off from an ongoing condition ---


def _row(
    engine: Engine,
    *,
    key: str,
    hours_ago: float,
    posted: bool = False,
    suppressed: bool = False,
    announce_clear: bool = False,
    title: str = "Daily-index reconciler is stalled",
) -> None:
    """Seed one outbox row at a chosen age and delivery state: pending, posted, or coalesced."""
    at = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    with engine.begin() as conn:
        insert_alert(
            conn,
            severity=ALERT,
            title=title,
            dedup_key=key,
            created_at=at,
            announce_clear=announce_clear,
        )
        if posted or suppressed:
            conn.execute(
                text(
                    "UPDATE alerts_outbox SET delivered_at = :at, attempts = :n "
                    "WHERE id = (SELECT MAX(id) FROM alerts_outbox)"
                ),
                {"at": at, "n": 1 if posted else 0},
            )


def test_a_condition_firing_more_often_than_the_window_still_repages() -> None:
    """The bug that kept a 26-day stall to a single page: each coalesced row was stamped
    delivered and restarted the window, so an hourly condition never re-paged. Only a real post
    may open the window."""
    engine = _fresh_db()
    _row(engine, key="ingest_cursor_stale", hours_ago=5, posted=True)
    for h in (4, 3, 2, 1):
        _row(engine, key="ingest_cursor_stale", hours_ago=h, suppressed=True)
    _row(engine, key="ingest_cursor_stale", hours_ago=0)
    notifier = FakeNotifier()

    counts = _drain_once(engine, notifier, repeat_hours=4.0)

    assert counts["delivered"] == 1
    note = notifier.sent[0][1]
    assert note.status == "ongoing"
    assert note.episode is not None
    assert note.episode.occurrences == 6
    first_row_at = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    assert note.episode.first_seen[:13] == first_row_at[:13]  # the episode began 5 hours ago


def test_first_trip_is_new_with_one_occurrence() -> None:
    engine = _fresh_db()
    _row(engine, key="classify_failure:anthropic_credit_exhausted", hours_ago=0)
    notifier = FakeNotifier()

    _drain_once(engine, notifier)

    note = notifier.sent[0][1]
    assert note.status == "new"
    assert note.episode is not None and note.episode.occurrences == 1


def test_a_silence_longer_than_the_window_starts_a_new_episode() -> None:
    engine = _fresh_db()
    _row(engine, key="k", hours_ago=10, posted=True)  # an earlier, finished episode
    _row(engine, key="k", hours_ago=0)
    notifier = FakeNotifier()

    _drain_once(engine, notifier, repeat_hours=4.0)

    note = notifier.sent[0][1]
    assert note.status == "new"
    assert note.episode is not None and note.episode.occurrences == 1


def test_keyless_alert_carries_no_episode() -> None:
    engine = _fresh_db()
    _seed(engine, severity=ALERT, title="panic")
    notifier = FakeNotifier()

    _drain_once(engine, notifier)

    note = notifier.sent[0][1]
    assert note.status is None and note.episode is None


def test_opted_in_condition_announces_its_clearing_once() -> None:
    engine = _fresh_db()
    _row(engine, key="classify_backlog", hours_ago=6, posted=True, announce_clear=True)
    _row(engine, key="classify_backlog", hours_ago=5, suppressed=True, announce_clear=True)
    notifier = FakeNotifier()

    counts = _drain_once(engine, notifier, repeat_hours=4.0)

    assert counts["cleared"] == 1
    note = notifier.sent[0][1]
    assert note.status == "cleared"
    assert note.title == "Daily-index reconciler is stalled"  # the condition's own headline
    assert note.episode is not None and note.episode.occurrences == 2

    # The next pass must not announce the same clearing again.
    notifier2 = FakeNotifier()
    assert _drain_once(engine, notifier2, repeat_hours=4.0)["cleared"] == 0
    assert notifier2.sent == []


def test_condition_still_firing_is_not_cleared() -> None:
    engine = _fresh_db()
    _row(engine, key="classify_backlog", hours_ago=6, posted=True, announce_clear=True)
    _row(engine, key="classify_backlog", hours_ago=1, suppressed=True, announce_clear=True)

    assert _drain_once(engine, FakeNotifier(), repeat_hours=4.0)["cleared"] == 0


def test_key_not_opted_in_is_never_announced_cleared() -> None:
    """Per-entity keys from before this change are all quiet; announcing each would be a storm."""
    engine = _fresh_db()
    _row(engine, key="classification_abandoned:0001-26-000001", hours_ago=6, posted=True)

    assert _drain_once(engine, FakeNotifier(), repeat_hours=4.0)["cleared"] == 0


def test_condition_quiet_beyond_the_lookback_is_not_announced() -> None:
    engine = _fresh_db()
    _row(engine, key="classify_backlog", hours_ago=24 * 10, posted=True, announce_clear=True)

    assert _drain_once(engine, FakeNotifier(), repeat_hours=4.0)["cleared"] == 0


def test_a_recurrence_after_clearing_is_new_and_clears_again() -> None:
    engine = _fresh_db()
    _row(engine, key="k", hours_ago=12, posted=True, announce_clear=True)
    _drain_once(engine, FakeNotifier(), repeat_hours=4.0)  # announces the first clearing
    with engine.begin() as conn:  # age that notice to match the episode it closed
        old = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
        conn.execute(
            text("UPDATE alerts_outbox SET created_at = :old WHERE dedup_key = 'k#cleared'"),
            {"old": old},
        )
    _row(engine, key="k", hours_ago=0, announce_clear=True)
    notifier = FakeNotifier()

    _drain_once(engine, notifier, repeat_hours=4.0)

    assert [n.status for _, n in notifier.sent] == ["new"]

    with engine.begin() as conn:  # let the recurrence go quiet too
        old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        conn.execute(
            text(
                "UPDATE alerts_outbox SET created_at = :old, delivered_at = :old "
                "WHERE dedup_key = 'k'"
            ),
            {"old": old},
        )
    notifier2 = FakeNotifier()
    assert _drain_once(engine, notifier2, repeat_hours=4.0)["cleared"] == 1
    assert [n.status for _, n in notifier2.sent] == ["cleared"]


class _LeakyNotifier:
    def send(self, webhook_url: str, notification: Notification) -> None:
        raise RuntimeError(
            "Client error '429 Too Many Requests' for url "
            "'https://discord.com/api/webhooks/1516106895679684780/SECRET-TOKEN-abc'"
        )


def test_webhook_url_is_redacted_from_the_stored_error() -> None:
    engine = _fresh_db()
    _seed(engine, severity=ALERT, title="will fail")

    _drain(
        engine,
        _LeakyNotifier(),
        CHANNELS,
        limit=None,
        info_ttl_minutes=30.0,
        repeat_hours=4.0,
        dry_run=False,
    )

    with engine.begin() as conn:
        last_error = conn.execute(text("SELECT last_error FROM alerts_outbox")).scalar_one()
    assert "SECRET-TOKEN" not in last_error
    assert "429 Too Many Requests" in last_error  # the useful part survives
    assert "redacted" in last_error
