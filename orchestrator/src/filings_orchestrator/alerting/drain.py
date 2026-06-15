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

**Dedup.** A pending row whose `dedup_key` already has a delivered sibling is
retired without posting — the condition already paged for its window (the window
lives in the key, e.g. one cost-cap alert per UTC day). Within a single pass,
the first row for a key delivers and later ones coalesce.

Needs the DB path always; needs the two webhook URLs only for a real pass
(`--dry-run` does not). Output is JSON-line structured events to stdout; exits
non-zero if any delivery POST failed (so the timer surfaces a stuck channel).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import Engine

from filings_orchestrator.alerting.emit import ALERT, INFO
from filings_orchestrator.alerting.notify import DiscordNotifier, Notification, Notifier
from filings_orchestrator.alerting.outbox import (
    PendingAlert,
    delivered_dedup_keys,
    fetch_undelivered_alerts,
    mark_alert_delivered,
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
    dry_run: bool,
) -> dict[str, int]:
    """Run one delivery pass; return the per-outcome counts."""
    now = datetime.now(UTC)
    pending = fetch_undelivered_alerts(engine, limit=limit)

    # One query for the whole batch: which dedup_keys already paged.
    candidate_keys = [a.dedup_key for a in pending if a.dedup_key]
    already_paged = delivered_dedup_keys(engine, candidate_keys)
    paged_this_pass: set[str] = set()

    delivered = suppressed_stale = suppressed_dup = failed = unroutable = 0

    for alert in pending:
        if _is_stale(alert, now, info_ttl_minutes):
            emit("alert_suppressed", id=alert.id, reason="stale", severity=alert.severity)
            if not dry_run:
                mark_alert_delivered(engine, alert.id, count_attempt=False)
            suppressed_stale += 1
            continue

        if alert.dedup_key and (
            alert.dedup_key in already_paged or alert.dedup_key in paged_this_pass
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

        notification = Notification(
            severity=alert.severity,
            title=alert.title,
            body=alert.body,
            fields=alert.fields,
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
            record_alert_delivery_failure(engine, alert.id, f"{type(exc).__name__}: {exc}")
            emit(
                "alert_delivery_failed",
                id=alert.id,
                severity=alert.severity,
                error_class=type(exc).__name__,
                message=str(exc),
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

    emit(
        "alarm_drain_started",
        dry_run=args.dry_run,
        limit=args.limit,
        info_ttl_minutes=info_ttl_minutes,
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
        dry_run=args.dry_run,
    )

    emit("alarm_drain_completed", **counts)
    if counts["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
