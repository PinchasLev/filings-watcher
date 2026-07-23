"""CLI: cursor-driven, resumable periodic-filing (10-K) ingest for change-detection.

Reads the periodic-ingest cursor, fetches each EDGAR daily-index file from the
cursor's date through today (Eastern Time), filters to 10-K, resolves each to its
primary document, segments Item 1A (Risk Factors) into whole risk-factor blocks
(ADR 0042), and stores the blocks plus a `periodic_filings` envelope row.
Deterministic — **no LLM** (no cost cap, no classifier slice); embeddings and the
materiality judge are later PRs.

Robustness mirrors the Form-4 path (ADR 0038):
- The envelope is the dedup anchor + completeness ledger — one row per PROCESSED
  10-K, written even when the document yields no blocks (non-markup, oversized, or
  no locatable section), so dedup keys off it and such filings are not re-fetched.
- The cursor advances past an index date ONLY once that date is fully ingested (no
  errors, nothing deferred by the per-tick cap). An aborted tick never reaches the
  advance, so the next run resumes from the incomplete date and fills the gap.

Scope: 10-K only (exact match, so 10-K/A amendments are excluded); MD&A and 10-Q
are later arcs. A large 10-K primary document beyond the parse-size cap is skipped
by the ADR 0040 guard rather than parsed.

Run as a one-shot (a systemd timer wiring is a separate infra step). Output is
JSON-line events to stdout.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
from opentelemetry import trace
from sqlalchemy import Engine

from filings_orchestrator.change_detection import segment_risk_factors
from filings_orchestrator.config import (
    MissingConfigError,
    get_config_int,
    get_config_str,
    require_env,
)
from filings_orchestrator.edgar import EdgarClient
from filings_orchestrator.edgar.daily_index import (
    DailyIndexEntry,
    fetch_daily_index,
    filter_form,
    parse_daily_index,
)
from filings_orchestrator.edgar.document import fetch_markup_text
from filings_orchestrator.edgar.filing_resolver import resolve_filing
from filings_orchestrator.log_events import emit
from filings_orchestrator.otel_setup import setup_otel
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import (
    advance_periodic_cursor,
    insert_periodic_filing,
    read_periodic_cursor,
    select_seen_periodic_accessions,
)

_EDGAR_RATE_LIMIT_PER_SEC = 2
_EASTERN = ZoneInfo("America/New_York")
_FORM = "10-K"
# Safety cap on a single tick's filings across all scanned dates. 10-Ks are far
# fewer than Form 4s but each is two fetches plus a heavy HTML parse, so the
# default drains a normal day in one run; a backlog drains across runs, the cursor
# only advancing past fully-ingested dates. Tunable via env.
_DEFAULT_MAX_PERIODIC_PER_TICK = 400
# Effectively unlimited per-run budget for a backfill (which drains the full range).
_UNCAPPED = 10**12


def _parse_filed_at_to_date(filed_at: str) -> date:
    return date.fromisoformat(filed_at[:10])


def _dates_to_scan(cursor_filed_at: str | None, today_et: date) -> list[date]:
    """Business dates to fetch this tick. First-ever tick (cursor unset): today only,
    no backfill. Subsequent ticks: from the cursor's date through today inclusive, so
    a partial/missed day self-heals (mirrors ADR 0021/0038)."""
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


def _date_range(since: date, until: date) -> list[date]:
    """All dates in [since, until], newest first — the backfill scan order, so the
    most recent (most relevant) history lands first if the run is interrupted."""
    out: list[date] = []
    current = until
    while current >= since:
        out.append(current)
        current = date.fromordinal(current.toordinal() - 1)
    return out


def _process_entry(
    client: EdgarClient, engine: Engine, entry: DailyIndexEntry, ingested_at: str
) -> int:
    """Resolve, fetch, segment, and store one 10-K. Returns the block count stored."""
    filing = resolve_filing(
        cik=entry.cik,
        accession_number=entry.accession_number,
        company_name=entry.company_name,
        form=entry.form,
        filed_at=entry.filed_at,
        client=client,
    )
    markup = fetch_markup_text(client, filing.primary_document_url)
    if markup is None:
        blocks = []
        parsed = False
        emit(
            "periodic_document_skipped",
            accession_number=entry.accession_number,
            reason="non-markup or oversized",
        )
    else:
        blocks = segment_risk_factors(markup)
        parsed = True
        if not blocks:
            emit(
                "periodic_no_blocks",
                accession_number=entry.accession_number,
                reason="no risk-factor section located",
            )
    insert_periodic_filing(
        engine,
        accession_number=entry.accession_number,
        cik=entry.cik,
        company_name=entry.company_name,
        form=entry.form,
        filed_at=entry.filed_at,
        period_of_report=filing.report_date.isoformat() if filing.report_date else None,
        fiscal_year=filing.report_date.year if filing.report_date else None,
        parsed=parsed,
        blocks=blocks,
        ingested_at=ingested_at,
    )
    return len(blocks)


def main() -> None:
    setup_otel()
    parser = argparse.ArgumentParser(
        prog="scan-periodic",
        description="Ingest 10-K risk-factor blocks from the EDGAR daily index (ADR 0042).",
    )
    parser.add_argument(
        "--date",
        help="Ingest only this index date (YYYY-MM-DD, ET); a manual override that "
        "does not read or advance the cursor. Default: cursor-driven scan to today (ET).",
    )
    parser.add_argument(
        "--since",
        help="Backfill: ingest the whole date range [--since, --until] newest-first, "
        "uncapped, without reading or advancing the cursor. Requires/pairs with --until.",
    )
    parser.add_argument(
        "--until",
        help="End of the --since backfill range (YYYY-MM-DD, ET). Defaults to today (ET).",
    )
    parser.add_argument(
        "--rate",
        type=int,
        default=_EDGAR_RATE_LIMIT_PER_SEC,
        help=f"EDGAR requests/sec (default {_EDGAR_RATE_LIMIT_PER_SEC}); raise for a "
        "one-off backfill (EDGAR's limit is 10).",
    )
    args = parser.parse_args()

    try:
        edgar_user_agent = require_env("EDGAR_USER_AGENT")
    except MissingConfigError as e:
        emit("tick_failed", source="periodic", error_class="MissingConfigError", message=str(e))
        sys.exit(2)

    db_path = get_config_str("FILINGS_DB_PATH", default="/var/lib/filings-watcher/filings.db")
    max_per_tick = get_config_int("MAX_PERIODIC_PER_TICK", _DEFAULT_MAX_PERIODIC_PER_TICK)
    today_et = datetime.now(_EASTERN).date()
    engine = open_engine(db_path)

    if args.date is not None and (args.since is not None or args.until is not None):
        emit(
            "tick_failed",
            source="periodic",
            error_class="ValueError",
            message="--date cannot be combined with --since/--until",
        )
        sys.exit(2)
    if args.until is not None and args.since is None:
        emit(
            "tick_failed",
            source="periodic",
            error_class="ValueError",
            message="--until requires --since",
        )
        sys.exit(2)

    range_mode = args.since is not None
    if args.since is not None:
        since = date.fromisoformat(args.since)
        until = date.fromisoformat(args.until) if args.until else today_et
        if since > until:
            emit(
                "tick_failed",
                source="periodic",
                error_class="ValueError",
                message=f"--since {since.isoformat()} is after --until {until.isoformat()}",
            )
            sys.exit(2)
        target_dates = _date_range(since, until)
        use_cursor = False
    elif args.date is not None:
        target_dates = [date.fromisoformat(args.date)]
        use_cursor = False
    else:
        use_cursor = True
        cursor = read_periodic_cursor(engine)
        target_dates = _dates_to_scan(cursor[1] if cursor else None, today_et)

    tracer = trace.get_tracer("filings_orchestrator")
    with tracer.start_as_current_span("tick") as span:
        started = datetime.now(UTC)
        emit(
            "tick_started",
            source="periodic",
            started_at=started.isoformat(),
            cursor_driven=use_cursor,
            backfill=range_mode,
            rate_per_sec=args.rate,
            dates_to_scan=len(target_dates),
        )

        filings_count = 0
        blocks_count = 0
        errors_count = 0
        total_deferred = 0
        dates_completed = 0
        today_publication_missing = False
        budget = _UNCAPPED if range_mode else max_per_tick
        ingested_at = datetime.now(UTC).isoformat()

        with EdgarClient(user_agent=edgar_user_agent, rate_limit_per_second=args.rate) as client:
            for target in target_dates:
                if budget <= 0:
                    break
                try:
                    index_text = fetch_daily_index(target, client)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (403, 404):
                        emit(
                            "tick_skipped_date",
                            source="periodic",
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
                seen = select_seen_periodic_accessions(
                    engine, [e.accession_number for e in entries]
                )
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
                        blocks_count += _process_entry(client, engine, entry, ingested_at)
                        filings_count += 1
                    except Exception as exc:
                        errors_count += 1
                        date_error = True
                        emit(
                            "periodic_failed",
                            accession_number=entry.accession_number,
                            error_class=type(exc).__name__,
                            message=str(exc),
                        )

                date_deferred = len(new_entries) - processed_in_date
                total_deferred += date_deferred
                emit(
                    "periodic_index_polled",
                    index_date=target.isoformat(),
                    entries_total=len(entries),
                    entries_new=len(new_entries),
                    entries_deferred=date_deferred,
                )

                date_complete = not date_error and date_deferred == 0
                if use_cursor and date_complete and entries:
                    high_water = max(entries, key=lambda e: (e.filed_at, e.accession_number))
                    advance_periodic_cursor(
                        engine, high_water.accession_number, high_water.filed_at
                    )
                    emit(
                        "cursor_advanced",
                        accession_number=high_water.accession_number,
                        filed_at=high_water.filed_at,
                    )
                if date_complete:
                    dates_completed += 1
                elif use_cursor:
                    break

        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        span.set_attribute("source", "periodic")
        span.set_attribute("filings_count", filings_count)
        span.set_attribute("blocks_count", blocks_count)
        span.set_attribute("errors_count", errors_count)
        emit(
            "tick_completed",
            source="periodic",
            duration_ms=duration_ms,
            dates_scanned=len(target_dates),
            dates_completed=dates_completed,
            filings_count=filings_count,
            blocks_count=blocks_count,
            errors_count=errors_count,
            entries_deferred=total_deferred,
            today_publication_missing=today_publication_missing,
        )


def _fail(started: datetime, **fields: object) -> None:
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    emit("tick_failed", source="periodic", duration_ms=duration_ms, **fields)
    sys.exit(1)


if __name__ == "__main__":
    main()
