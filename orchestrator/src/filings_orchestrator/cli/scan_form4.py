"""CLI: one Form-4 (insider transaction) daily-index ingest tick.

Reads the EDGAR daily index for a target date, filters to Form 4, and parses each
into the `insider_transactions` table — the foundation for the insider-activity
signal. Deterministic XML parsing, so **no LLM**: no cost cap, no classifier slice.
Idempotent on (accession_number, txn_seq).

US-domestic only by construction: foreign private issuers (6-K filers) are exempt
from Section 16 and file no Form 4.

v1 runs off the once-daily index — insider data is not latency-critical, and this
is the backfill path too. Near-real-time atom ingest is a deferred follow-up: the
`getcurrent?type=4` atom entry's CIK is the reporting owner, which does not host the
full-submission `.txt`, so the document URL needs separate resolution.

Run as a one-shot under a systemd timer in the evening cluster (after EDGAR
publishes the daily index ~10 PM ET). Output is JSON-line events to stdout.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
from opentelemetry import trace

from filings_orchestrator.config import (
    MissingConfigError,
    get_config_int,
    get_config_str,
    require_env,
)
from filings_orchestrator.edgar import EdgarClient
from filings_orchestrator.edgar.daily_index import (
    fetch_daily_index,
    filter_form,
    parse_daily_index,
)
from filings_orchestrator.edgar.form4 import fetch_form4_submission, parse_form4
from filings_orchestrator.log_events import emit
from filings_orchestrator.otel_setup import setup_otel
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import (
    insert_insider_transactions,
    select_seen_insider_accessions,
)

_EDGAR_RATE_LIMIT_PER_SEC = 2
_EASTERN = ZoneInfo("America/New_York")
_FORM = "4"
# Safety cap on a single tick's parse volume. ~1,000 Form 4s/day at observed volume,
# so the default drains a normal day in one run (each is a cheap fetch+parse, no LLM);
# a backlog/backfill larger than this drains across runs. Tunable via env.
_DEFAULT_MAX_FORM4_PER_TICK = 1500


def main() -> None:
    setup_otel()
    parser = argparse.ArgumentParser(
        prog="scan-form4",
        description="Ingest Form 4 insider transactions from the EDGAR daily index.",
    )
    parser.add_argument("--date", help="Index date YYYY-MM-DD (ET). Default: today (ET).")
    args = parser.parse_args()

    try:
        edgar_user_agent = require_env("EDGAR_USER_AGENT")
    except MissingConfigError as e:
        emit("tick_failed", source="form4", error_class="MissingConfigError", message=str(e))
        sys.exit(2)

    db_path = get_config_str("FILINGS_DB_PATH", default="/var/lib/filings-watcher/filings.db")
    max_per_tick = get_config_int("MAX_FORM4_PER_TICK", _DEFAULT_MAX_FORM4_PER_TICK)
    target = date.fromisoformat(args.date) if args.date else datetime.now(_EASTERN).date()
    engine = open_engine(db_path)

    tracer = trace.get_tracer("filings_orchestrator")
    with tracer.start_as_current_span("tick") as span:
        started = datetime.now(UTC)
        emit(
            "tick_started",
            source="form4",
            started_at=started.isoformat(),
            index_date=target.isoformat(),
        )

        filings_count = 0
        transactions_count = 0
        skipped = 0
        errors_count = 0
        entries_deferred = 0

        with EdgarClient(
            user_agent=edgar_user_agent,
            rate_limit_per_second=_EDGAR_RATE_LIMIT_PER_SEC,
        ) as client:
            try:
                index_text = fetch_daily_index(target, client)
            except httpx.HTTPStatusError as exc:
                # EDGAR returns 403/404 for an unpublished index (today before the
                # ~10 PM ET batch, weekends, holidays) — skip and let the next tick retry.
                if exc.response.status_code in (403, 404):
                    emit(
                        "tick_skipped_date",
                        source="form4",
                        date=target.isoformat(),
                        status=exc.response.status_code,
                        reason="daily index not published",
                    )
                    return
                _fail(
                    started,
                    error_class=type(exc).__name__,
                    message=f"HTTP {exc.response.status_code} fetching {target.isoformat()}",
                )
                return

            entries = filter_form(parse_daily_index(index_text), _FORM)
            seen = select_seen_insider_accessions(engine, [e.accession_number for e in entries])
            new_entries = [e for e in entries if e.accession_number not in seen]
            batch = new_entries[:max_per_tick]
            entries_deferred = len(new_entries) - len(batch)
            emit(
                "form4_index_polled",
                index_date=target.isoformat(),
                entries_total=len(entries),
                entries_new=len(new_entries),
                entries_deferred=entries_deferred,
            )

            ingested_at = datetime.now(UTC).isoformat()
            for entry in batch:
                try:
                    submission = fetch_form4_submission(client, entry.submission_path)
                    filing = parse_form4(submission, entry.accession_number)
                    if filing is None:
                        skipped += 1
                        emit(
                            "form4_skipped",
                            accession_number=entry.accession_number,
                            reason="no ownership document",
                        )
                        continue
                    transactions_count += insert_insider_transactions(
                        engine, filing, filed_at=entry.filed_at, ingested_at=ingested_at
                    )
                    filings_count += 1
                except Exception as exc:
                    # Continue-on-failure: one bad filing must not abort a cheap parse
                    # tick (no cost cap to protect, unlike the classify ticks).
                    errors_count += 1
                    emit(
                        "form4_failed",
                        accession_number=entry.accession_number,
                        error_class=type(exc).__name__,
                        message=str(exc),
                    )
                    continue

        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        span.set_attribute("source", "form4")
        span.set_attribute("filings_count", filings_count)
        span.set_attribute("transactions_count", transactions_count)
        span.set_attribute("errors_count", errors_count)
        emit(
            "tick_completed",
            source="form4",
            index_date=target.isoformat(),
            duration_ms=duration_ms,
            filings_count=filings_count,
            transactions_count=transactions_count,
            skipped=skipped,
            errors_count=errors_count,
            entries_deferred=entries_deferred,
        )


def _fail(started: datetime, **fields: object) -> None:
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    emit("tick_failed", source="form4", duration_ms=duration_ms, **fields)
    sys.exit(1)


if __name__ == "__main__":
    main()
