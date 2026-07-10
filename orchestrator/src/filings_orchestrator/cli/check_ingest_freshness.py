"""CLI: alarm when the daily-index reconciler cursor has fallen behind (ADR 0041).

The existing "daily index not published" alarm (scan_daily_index) fires only when
*today's* index is missing at the end of the evening cluster. It does not catch
the failure this check exists for: the index publishes fine, but the reconciler
keeps dying before it can advance — so the cursor sits days behind while today's
data flows in via the atom feed. That is exactly the 2026-07-10 incident (a poison
PDF OOM-killed every daily-index tick for ~2 days), and nothing paged.

This is a dead-man's switch on the *outcome*, not the cause. It reads only the
cursor and the calendar — no EDGAR fetch, no parse, no LLM — so it cannot itself
OOM or hang the way a tick can, and it pages regardless of *why* the cursor stalled
(OOM, hang, cost-cap wedge, a timer that never armed). It runs on its own light
timer, independent of the ingest path it watches.

**Staleness in business days, not wall-clock.** The cursor legitimately sits at
"yesterday" between evening ticks and at "last Friday" all weekend; a wall-clock
age would cry wolf every weekend and holiday. So lag is counted in fully-elapsed
business days between the cursor's date and today (see `business_days_between`),
and the threshold carries enough slack (default 3) that the weekday-only calendar's
holiday over-count cannot trip it. A genuine multi-day stall (the incident was ~8
business days) clears it comfortably.

Reads only `FILINGS_DB_PATH`. Emits a structured heartbeat event every run and,
when stale, one outbox alert (per-cause dedup key, so a standing stall pages once
per drainer repeat window, not every tick). Output is JSON-line events to stdout.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from filings_orchestrator.alerting import ALERT, emit_alert
from filings_orchestrator.business_days import business_days_between, parse_filed_at_to_date
from filings_orchestrator.config import get_config_int, get_config_str
from filings_orchestrator.log_events import emit
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import read_ingest_cursor

_EASTERN = ZoneInfo("America/New_York")

# Business-day lag at or beyond which the reconciler cursor is considered stalled.
# 3 gives slack for the normal 1-day evening cadence, an occasional skipped night,
# and the weekday-only calendar's holiday over-count (the largest routine US market
# closure is two consecutive weekdays), while still catching a real multi-day stall.
# Tightening this to 2 for faster detection wants a holiday-aware calendar first —
# see business_days.py. Env-overridable to raise it during an extraordinary closure.
_DEFAULT_STALE_BUSINESS_DAYS = 3


def run_check(engine: Engine, today_et: date, threshold: int) -> None:
    """Evaluate cursor staleness against `today_et`; emit heartbeat + alert-if-stale.

    Factored out of `main` so the lag math and alarm decision are unit-testable
    against a seeded cursor and a fixed date, without patching the clock.
    """
    cursor = read_ingest_cursor(engine)

    if cursor is None:
        # No cursor yet (a fresh install before the first daily-index tick). We
        # cannot distinguish "never ran" from "just installed" here, so we do not
        # alarm — a wholly dead system is caught by the host/atom signals. Emit the
        # heartbeat so the check's own liveness is visible in the log.
        emit("ingest_freshness_checked", cursor_set=False, threshold_business_days=threshold)
        return

    cursor_filed_at = cursor[1]
    cursor_date = parse_filed_at_to_date(cursor_filed_at)
    lag = business_days_between(cursor_date, today_et)
    stale = lag >= threshold

    emit(
        "ingest_freshness_checked",
        cursor_set=True,
        cursor_filed_at=cursor_filed_at,
        today=today_et.isoformat(),
        lag_business_days=lag,
        threshold_business_days=threshold,
        stale=stale,
    )

    if stale:
        # Per-cause dedup: one stall is one page, then the drainer re-pages once per
        # ALERT_REPEAT_HOURS while it persists, and goes silent once the cursor
        # catches up and this stops emitting. The key omits the date deliberately so
        # a cursor that inches forward but stays behind does not re-page on each step.
        emit_alert(
            engine,
            ALERT,
            "Daily-index reconciler is stalled",
            body=(
                f"The daily-index ingest cursor is at {cursor_date.isoformat()} — "
                f"{lag} business days behind today ({today_et.isoformat()}), at or over the "
                f"{threshold}-business-day threshold. Today's filings may still be flowing via "
                f"the atom feed, but the daily-index backstop has stopped advancing, so filings "
                f"it alone would reconcile are being missed. Check the daily-index tick logs "
                f"(journalctl -u filings-daily-index) for OOM (status=137), a hang, or a "
                f"cost-cap wedge, and confirm the timer is armed."
            ),
            dedup_key="ingest_cursor_stale",
            cursor_filed_at=cursor_filed_at,
            lag_business_days=lag,
            threshold_business_days=threshold,
            today=today_et.isoformat(),
        )


def main() -> None:
    db_path = get_config_str("FILINGS_DB_PATH", default="/var/lib/filings-watcher/filings.db")
    threshold = get_config_int("INGEST_CURSOR_STALE_BUSINESS_DAYS", _DEFAULT_STALE_BUSINESS_DAYS)
    engine = open_engine(db_path)
    today_et = datetime.now(_EASTERN).date()
    run_check(engine, today_et, threshold)


if __name__ == "__main__":
    main()
