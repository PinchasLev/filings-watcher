"""alarm-drain — deliver queued alerts to Discord (ADR 0031).

The sole Discord-aware component. A systemd timer fires it on a short interval;
it reads undelivered rows from `alerts_outbox`, POSTs each to the right channel
by severity, and marks it delivered. Pure delivery — it carries no detection
logic and never classifies; producers elsewhere (the reconciler, the Go panic
handler, future absence checks) put rows in the outbox, and this drains them.

    uv run alarm-drain              # one delivery pass, then exit
    uv run alarm-drain --dry-run    # report what would be sent/suppressed, no POSTs
    uv run alarm-drain --limit 50   # bound a single pass

Single pass, not a daemon: re-running is the timer's job, which keeps this lean
and crash-safe (an undelivered row simply waits for the next firing — the
at-least-once half of the outbox).

**Severity -> channel routing.** The producer sets a severity; the drainer owns
the policy of what to do with it. `alert` -> the #alerts webhook (needs action),
`info` -> the #info webhook (situational awareness). Webhook URLs come from the
environment (SSM-sourced SecureStrings on the host).

**Freshness window.** Because rows accumulate while no drainer runs (first
deploy, or any drainer outage), a naive pass would flood the channel with stale
backlog. So delivery is severity-aware: an `info` row older than
`ALERT_INFO_TTL_MINUTES` (default 30) is retired WITHOUT posting — stale
situational awareness has no value late. `alert` rows have no TTL: an old
dead-letter still needs a human, and `dedup_key` already collapses repeats.

**Dedup + renotification.** A pending row whose `dedup_key` was delivered within
the last `ALERT_REPEAT_HOURS` (default 4) is retired without posting — so a
single root cause that keeps tripping (a hot-looping panic, a standing absence
condition) pages at most once per window instead of on every occurrence. Once
the window lapses, a still-firing condition re-pages as a "still broken"
reminder; when the condition clears the producer simply stops emitting and the
reminders stop. Within a single pass, the first row for a key delivers and later
ones coalesce.

**Episodes, so one message tells a one-off from an ongoing condition.** Every page
for a keyed alert carries the key's current episode, reconstructed from the outbox
itself: first seen, occurrences, last seen, and a status — `new` for the first
trip, `ongoing` for a reminder. A key that opted in (`announce_clear`) also gets a
`cleared` message once it has been quiet for the repeat window, so a one-off reads
as a page then an all-clear, and an ongoing condition as reminders whose duration
and count keep growing. "Cleared" means the producer stopped reporting it — the
condition stopped tripping, or the producer stopped running; a dead host is the
heartbeat's job to catch. Choose the key at the granularity the operator acts on: per-entity
for "this filing needs review", per-cause for "the system is broken".

Needs the DB path always; needs the two webhook URLs only for a real pass
(`--dry-run` does not). Output is JSON-line structured events to stdout; exits
non-zero if any delivery POST failed (so the timer surfaces a stuck channel).
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import Engine

from filings_orchestrator.alerting.emit import ALERT, INFO
from filings_orchestrator.alerting.notify import (
    CLEARED,
    NEW,
    ONGOING,
    DiscordNotifier,
    Notification,
    Notifier,
)
from filings_orchestrator.alerting.outbox import (
    CLEARED_SUFFIX,
    Episode,
    PendingAlert,
    current_episode,
    delivered_dedup_keys,
    fetch_undelivered_alerts,
    insert_alert,
    mark_alert_delivered,
    quiet_conditions,
    record_alert_delivery_failure,
)
from filings_orchestrator.config import (
    MissingConfigError,
    get_config_float,
    get_config_str,
    get_secret,
)
from filings_orchestrator.log_events import emit
from filings_orchestrator.persistence import open_engine

_DEFAULT_INFO_TTL_MINUTES = 30.0
_DEFAULT_REPEAT_HOURS = 4.0

# How far back a quiet condition can still be announced as cleared. Bounds the scan, and keeps an
# old episode from being announced long after anyone cared about it.
_CLEAR_LOOKBACK = timedelta(days=7)

# A webhook URL is a credential: its token is the whole of the authorization. httpx quotes the URL
# in its error text, so it is redacted before the error is stored anywhere an operator can read.
_WEBHOOK_URL = re.compile(r"https://(?:discord|discordapp)\.com/api/webhooks/\S+")


def _redact(message: str) -> str:
    return _WEBHOOK_URL.sub("<discord webhook URL redacted>", message)


def _announce_clears(engine: Engine, now: datetime, window: timedelta, *, dry_run: bool) -> int:
    """Write a cleared notice for every opted-in condition that has gone quiet; return the count.

    Each notice is an ordinary outbox row, so this same pass delivers it, and a failed POST
    retries like any other.
    """
    due = quiet_conditions(
        engine,
        quiet_before=(now - window).isoformat(),
        seen_since=(now - _CLEAR_LOOKBACK).isoformat(),
    )
    for condition in due:
        emit("alert_cleared", dedup_key=condition.dedup_key, last_seen=condition.last_seen)
        if dry_run:
            continue
        with engine.begin() as conn:
            insert_alert(
                conn,
                severity=condition.severity,
                title=condition.title,
                body=f"No new occurrences for {_hours(window)}.",
                dedup_key=condition.dedup_key + CLEARED_SUFFIX,
                created_at=now.isoformat(),
            )
    return len(due)


def _hours(window: timedelta) -> str:
    hours = window.total_seconds() / 3600
    return f"{hours:g} hour" + ("" if hours == 1 else "s")


def _status(engine: Engine, alert: PendingAlert, gap: timedelta) -> tuple[str, Episode] | None:
    """Where a keyed alert stands: new, ongoing, or the all-clear, with its episode."""
    if not alert.dedup_key:
        return None
    if alert.dedup_key.endswith(CLEARED_SUFFIX):
        episode = current_episode(engine, alert.dedup_key.removesuffix(CLEARED_SUFFIX), gap=gap)
        return (CLEARED, episode) if episode else None
    episode = current_episode(engine, alert.dedup_key, gap=gap)
    if episode is None:
        return None
    return (ONGOING if episode.first_seen < alert.created_at else NEW), episode


def _is_stale(alert: PendingAlert, now: datetime, info_ttl_minutes: float) -> bool:
    """True if an INFO row is past its TTL and should be dropped, not posted.

    ALERT rows never go stale (an old dead-letter is still actionable), so only
    INFO is subject to the freshness window.
    """
    if alert.severity != INFO:
        return False
    age = now - datetime.fromisoformat(alert.created_at)
    return age > timedelta(minutes=info_ttl_minutes)


def _drain(
    engine: Engine,
    notifier: Notifier,
    channel_url: dict[str, str],
    *,
    limit: int | None,
    info_ttl_minutes: float,
    repeat_hours: float,
    dry_run: bool,
) -> dict[str, int]:
    """Run one delivery pass; return the per-outcome counts."""
    now = datetime.now(UTC)
    window = timedelta(hours=repeat_hours)
    cleared = _announce_clears(engine, now, window, dry_run=dry_run)
    pending = fetch_undelivered_alerts(engine, limit=limit)

    # One query for the whole batch: which dedup_keys paged within the repeat
    # window. A key last delivered before the cutoff is eligible to re-page.
    repeat_cutoff = (now - timedelta(hours=repeat_hours)).isoformat()
    candidate_keys = [a.dedup_key for a in pending if a.dedup_key]
    already_paged = delivered_dedup_keys(engine, candidate_keys, delivered_since=repeat_cutoff)
    paged_this_pass: set[str] = set()

    delivered = suppressed_stale = suppressed_dup = failed = unroutable = 0

    for alert in pending:
        if _is_stale(alert, now, info_ttl_minutes):
            emit("alert_suppressed", id=alert.id, reason="stale", severity=alert.severity)
            if not dry_run:
                mark_alert_delivered(engine, alert.id, count_attempt=False)
            suppressed_stale += 1
            continue

        # A cleared notice is written once per episode by construction, so it is never a
        # duplicate of anything; coalescing it would swallow the all-clear for a second episode.
        if (
            alert.dedup_key
            and not alert.dedup_key.endswith(CLEARED_SUFFIX)
            and (alert.dedup_key in already_paged or alert.dedup_key in paged_this_pass)
        ):
            emit("alert_suppressed", id=alert.id, reason="duplicate", dedup_key=alert.dedup_key)
            if not dry_run:
                mark_alert_delivered(engine, alert.id, count_attempt=False)
            suppressed_dup += 1
            continue

        url = channel_url.get(alert.severity)
        if url is None:
            # Producers validate severity against the same set, so this is only
            # reachable via manual DB edits. Leave the row undelivered and shout
            # rather than silently retiring something we cannot route.
            emit("alert_unroutable", id=alert.id, severity=alert.severity)
            unroutable += 1
            continue

        standing = _status(engine, alert, window)
        notification = Notification(
            severity=alert.severity,
            title=alert.title,
            body=alert.body,
            fields=alert.fields,
            status=standing[0] if standing else None,
            episode=standing[1] if standing else None,
        )
        if dry_run:
            emit("alert_would_deliver", id=alert.id, severity=alert.severity, title=alert.title)
            delivered += 1
            if alert.dedup_key:
                paged_this_pass.add(alert.dedup_key)
            continue

        try:
            notifier.send(url, notification)
        except Exception as exc:  # keep draining; this row retries next pass
            message = _redact(str(exc))
            record_alert_delivery_failure(engine, alert.id, f"{type(exc).__name__}: {message}")
            emit(
                "alert_delivery_failed",
                id=alert.id,
                severity=alert.severity,
                error_class=type(exc).__name__,
                message=message,
            )
            failed += 1
            continue

        mark_alert_delivered(engine, alert.id, count_attempt=True)
        if alert.dedup_key:
            paged_this_pass.add(alert.dedup_key)
        emit("alert_delivered", id=alert.id, severity=alert.severity)
        delivered += 1

    return {
        "pending": len(pending),
        "delivered": delivered,
        "suppressed_stale": suppressed_stale,
        "suppressed_dup": suppressed_dup,
        "failed": failed,
        "unroutable": unroutable,
        "cleared": cleared,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="alarm-drain",
        description="Deliver queued alerts from the outbox to Discord.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be delivered/suppressed without POSTing or writing.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum rows to process in this pass (default: all undelivered).",
    )
    args = parser.parse_args()

    db_path = get_config_str("FILINGS_DB_PATH", default="/var/lib/filings-watcher/filings.db")
    engine = open_engine(db_path)
    info_ttl_minutes = get_config_float("ALERT_INFO_TTL_MINUTES", _DEFAULT_INFO_TTL_MINUTES)
    repeat_hours = get_config_float("ALERT_REPEAT_HOURS", _DEFAULT_REPEAT_HOURS)

    emit(
        "alarm_drain_started",
        dry_run=args.dry_run,
        limit=args.limit,
        info_ttl_minutes=info_ttl_minutes,
        repeat_hours=repeat_hours,
    )

    # --dry-run inspects routing/freshness without delivering, so it needs no
    # webhook credentials — mirror reclassify-orphans' --dry-run.
    channel_url: dict[str, str] = {}
    notifier: Notifier = DiscordNotifier(httpx.Client())
    if not args.dry_run:
        try:
            channel_url = {
                ALERT: get_secret("DISCORD_ALERTS_WEBHOOK_URL"),
                INFO: get_secret("DISCORD_INFO_WEBHOOK_URL"),
            }
        except MissingConfigError as exc:
            emit("alarm_drain_failed", error_class="MissingConfigError", message=str(exc))
            sys.exit(2)

    counts = _drain(
        engine,
        notifier,
        channel_url,
        limit=args.limit,
        info_ttl_minutes=info_ttl_minutes,
        repeat_hours=repeat_hours,
        dry_run=args.dry_run,
    )

    emit("alarm_drain_completed", **counts)
    if counts["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
