"""CLI: cursor-driven, resumable Form-4 (insider transaction) daily-index ingest.

Reads the Form-4 ingest cursor, fetches each EDGAR daily-index file from the
cursor's date through today (Eastern Time), filters to Form 4, parses each into
the `insider_transactions` table, and records every processed filing in the
`insider_filings` envelope. Deterministic XML parsing, so **no LLM**: no cost
cap, no classifier slice.

Robustness (ADR 0038, mirrors the 8-K daily-index path of ADR 0021):
- The envelope is the dedup anchor and completeness ledger — a row per processed
  Form 4, written even for option-only or unparseable filings, so dedup keys off
  it (not insider_transactions, which lacks rows for filings with no
  non-derivative transactions).
- The cursor advances past an index date ONLY once that date is fully ingested
  (no errors, nothing deferred by the per-tick budget). An aborted tick never
  reaches the advance, so the next run resumes from the incomplete date and
  fills the gap. Transient fetch failures hold the cursor and retry next run.

US-domestic only by construction: foreign private issuers (6-K filers) are
exempt from Section 16 and file no Form 4.

Near-real-time atom ingest is a deferred follow-up: the `getcurrent?type=4` atom
entry's CIK is the reporting owner, which does not host the full-submission
`.txt`, so the document URL needs separate resolution.

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
    advance_form4_cursor,
    insert_insider_derivative_transactions,
    insert_insider_filing,
    insert_insider_transactions,
    read_form4_cursor,
    select_seen_insider_accessions,
)

_EDGAR_RATE_LIMIT_PER_SEC = 2
_EASTERN = ZoneInfo("America/New_York")
_FORM = "4"
# Safety cap on a single tick's parse volume across all scanned dates. ~1,000
# Form 4s/day at observed volume, so the default drains a normal day in one run
# (each is a cheap fetch+parse, no LLM); a larger backlog drains across runs,
# the cursor only advancing past fully-ingested dates. Tunable via env.
_DEFAULT_MAX_FORM4_PER_TICK = 1500


def _parse_filed_at_to_date(filed_at: str) -> date:
    """Parse a daily-index `filed_at` (YYYY-MM-DD) to a date."""
    return date.fromisoformat(filed_at[:10])


def _dates_to_scan(cursor_filed_at: str | None, today_et: date) -> list[date]:
    """Business dates to fetch this tick.

    First-ever tick (cursor unset): scan today only — the first cursor advance
    is "now", no backfill (mirrors ADR 0021). Subsequent ticks: from the
    cursor's date through today inclusive, so a partial/missed day self-heals.
    """
    if cursor_filed_at is None:
        return [today_et]
    start = _parse_filed_at_to_date(cursor_filed_at)
    if start > today_et:
        return [today_et]
    out: list[date] = []
    current = start
    while current <= today_et:
        out.append(current)
        current = date.fromordinal(current.toordinal() + 1)
    return out


def main() -> None:
    setup_otel()
    parser = argparse.ArgumentParser(
        prog="scan-form4",
        description="Ingest Form 4 insider transactions from the EDGAR daily index.",
    )
    parser.add_argument(
        "--date",
        help="Ingest only this index date (YYYY-MM-DD, ET); a manual override that "
        "does not read or advance the cursor. Default: cursor-driven scan to today (ET).",
    )
    args = parser.parse_args()

    try:
        edgar_user_agent = require_env("EDGAR_USER_AGENT")
    except MissingConfigError as e:
        emit("tick_failed", source="form4", error_class="MissingConfigError", message=str(e))
        sys.exit(2)

    db_path = get_config_str("FILINGS_DB_PATH", default="/var/lib/filings-watcher/filings.db")
    max_per_tick = get_config_int("MAX_FORM4_PER_TICK", _DEFAULT_MAX_FORM4_PER_TICK)
    today_et = datetime.now(_EASTERN).date()
    engine = open_engine(db_path)

    use_cursor = args.date is None
    if use_cursor:
        cursor = read_form4_cursor(engine)
        target_dates = _dates_to_scan(cursor[1] if cursor else None, today_et)
    else:
        target_dates = [date.fromisoformat(args.date)]

    tracer = trace.get_tracer("filings_orchestrator")
    with tracer.start_as_current_span("tick") as span:
        started = datetime.now(UTC)
        emit(
            "tick_started",
            source="form4",
            started_at=started.isoformat(),
            cursor_driven=use_cursor,
            dates_to_scan=[d.isoformat() for d in target_dates],
        )

        filings_count = 0
        transactions_count = 0
        derivative_transactions_count = 0
        skipped = 0
        errors_count = 0
        total_deferred = 0
        dates_completed = 0
        today_publication_missing = False
        budget = max_per_tick
        ingested_at = datetime.now(UTC).isoformat()

        with EdgarClient(
            user_agent=edgar_user_agent,
            rate_limit_per_second=_EDGAR_RATE_LIMIT_PER_SEC,
        ) as client:
            for target in target_dates:
                if budget <= 0:
                    # Per-tick cap reached; leave remaining dates for the next run.
                    # The cursor was not advanced past them, so they are re-scanned.
                    break
                try:
                    index_text = fetch_daily_index(target, client)
                except httpx.HTTPStatusError as exc:
                    # EDGAR returns 403/404 for an unpublished index (today before the
                    # ~10 PM ET batch, weekends, holidays). Skip without advancing so
                    # the next tick retries; a non-business day re-checks cheaply until
                    # a later date with entries carries the cursor past it.
                    if exc.response.status_code in (403, 404):
                        emit(
                            "tick_skipped_date",
                            source="form4",
                            date=target.isoformat(),
                            status=exc.response.status_code,
                            reason="daily index not published",
                        )
                        if target == today_et:
                            today_publication_missing = True
                        continue
                    _fail(
                        started,
                        error_class=type(exc).__name__,
                        message=f"HTTP {exc.response.status_code} fetching {target.isoformat()}",
                    )
                    return

                entries = filter_form(parse_daily_index(index_text), _FORM)
                seen = select_seen_insider_accessions(engine, [e.accession_number for e in entries])
                new_entries = sorted(
                    (e for e in entries if e.accession_number not in seen),
                    key=lambda e: (e.filed_at, e.accession_number),
                )

                processed_in_date = 0
                date_error = False
                for entry in new_entries:
                    if budget <= 0:
                        break
                    budget -= 1
                    processed_in_date += 1
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
                            insert_insider_filing(
                                engine,
                                accession_number=entry.accession_number,
                                filed_at=entry.filed_at,
                                ingested_at=ingested_at,
                                filing=None,
                            )
                            continue
                        transactions_count += insert_insider_transactions(
                            engine, filing, filed_at=entry.filed_at, ingested_at=ingested_at
                        )
                        derivative_transactions_count += insert_insider_derivative_transactions(
                            engine, filing, filed_at=entry.filed_at, ingested_at=ingested_at
                        )
                        insert_insider_filing(
                            engine,
                            accession_number=entry.accession_number,
                            filed_at=entry.filed_at,
                            ingested_at=ingested_at,
                            filing=filing,
                            non_derivative_count=len(filing.transactions),
                            derivative_count=len(filing.derivative_transactions),
                        )
                        filings_count += 1
                    except Exception as exc:
                        # Continue within the date so one bad filing does not strand the
                        # rest, but flag the date so the cursor does not advance past it —
                        # a transient failure retries next run (the resume contract).
                        errors_count += 1
                        date_error = True
                        emit(
                            "form4_failed",
                            accession_number=entry.accession_number,
                            error_class=type(exc).__name__,
                            message=str(exc),
                        )

                date_deferred = len(new_entries) - processed_in_date
                total_deferred += date_deferred
                emit(
                    "form4_index_polled",
                    index_date=target.isoformat(),
                    entries_total=len(entries),
                    entries_new=len(new_entries),
                    entries_deferred=date_deferred,
                )

                date_complete = not date_error and date_deferred == 0
                if use_cursor and date_complete and entries:
                    high_water = max(entries, key=lambda e: (e.filed_at, e.accession_number))
                    advance_form4_cursor(engine, high_water.accession_number, high_water.filed_at)
                    emit(
                        "cursor_advanced",
                        accession_number=high_water.accession_number,
                        filed_at=high_water.filed_at,
                    )
                if date_complete:
                    dates_completed += 1
                elif use_cursor:
                    # Incomplete date (error or budget) — stop so the cursor stays put
                    # and the next run resumes here. Filling the gap is automatic.
                    break

        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        span.set_attribute("source", "form4")
        span.set_attribute("filings_count", filings_count)
        span.set_attribute("transactions_count", transactions_count)
        span.set_attribute("errors_count", errors_count)
        emit(
            "tick_completed",
            source="form4",
            duration_ms=duration_ms,
            dates_scanned=len(target_dates),
            dates_completed=dates_completed,
            filings_count=filings_count,
            transactions_count=transactions_count,
            derivative_transactions_count=derivative_transactions_count,
            skipped=skipped,
            errors_count=errors_count,
            entries_deferred=total_deferred,
            today_publication_missing=today_publication_missing,
        )


def _fail(started: datetime, **fields: object) -> None:
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    emit("tick_failed", source="form4", duration_ms=duration_ms, **fields)
    sys.exit(1)


if __name__ == "__main__":
    main()
