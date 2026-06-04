"""CLI: one daily-index ingest tick.

Reads the ingest cursor, fetches each EDGAR daily-index file between the
cursor's date (exclusive) and today (Eastern Time, inclusive), filters
to 8-Ks not yet present in the local DB, classifies each one with
Anthropic-side retries, reduces the classification into the filing-level
events layer, and advances the cursor per filing.

Per ADR 0021:
- Correctness on dedup comes from the accession-number primary key on
  filings, not the cursor.
- The cursor advances only past filings whose classification persisted.
- On exhausted retries for any filing, the tick logs `tick_failed` and
  exits non-zero; the next tick re-attempts from the failing filing.

The reduce stage (ADR 0027/0028) runs per filing as its own run. It is
derived and replayable, so a reduce failure is non-fatal: it is logged and
counted but does not fail the tick or hold the cursor — the classification
is already persisted and `reduce-corpus` closes any gap in the events layer.

Run as a one-shot under the systemd timer (per ADR 0012). Output is
JSON-line structured events to stdout, captured by journald.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
from opentelemetry import trace
from sqlalchemy import Engine, bindparam, text

from filings_orchestrator.classify import (
    FilingClassification,
    classify_filing,
    reduce_filing,
    reducer_version,
)
from filings_orchestrator.classify.retry import with_retries
from filings_orchestrator.config import MissingConfigError, load_config
from filings_orchestrator.cost import db_cost_sink, set_cost_sink
from filings_orchestrator.edgar import EdgarClient, fetch_filing_document
from filings_orchestrator.edgar.daily_index import (
    DailyIndexEntry,
    fetch_daily_index,
    filter_form,
    parse_daily_index,
    resolve_filing,
)
from filings_orchestrator.log_events import emit
from filings_orchestrator.otel_setup import setup_otel
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import (
    advance_ingest_cursor,
    complete_run,
    create_run,
    daily_cost_usd,
    insert_classifications,
    insert_events,
    lookup_ticker_by_cik,
    read_ingest_cursor,
    upsert_filing_document,
)

# EDGAR rate limit for the unattended ingest path. The published ceiling is
# 10 req/sec; 2 req/sec leaves significant headroom for retries and for the
# operator-on-demand `classify-filing` to share the limiter without
# interference. See ADR 0021 + ADR 0012.
_EDGAR_RATE_LIMIT_PER_SEC = 2

_EASTERN = ZoneInfo("America/New_York")


def main() -> None:
    setup_otel()

    try:
        config = load_config()
    except MissingConfigError as e:
        emit("tick_failed", error_class="MissingConfigError", message=str(e))
        sys.exit(2)

    os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    os.environ["LANGSMITH_API_KEY"] = config.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = config.langsmith_project
    os.environ["LANGSMITH_TRACING"] = "true" if config.langsmith_tracing else "false"

    tracer = trace.get_tracer("filings_orchestrator")
    with tracer.start_as_current_span("tick") as span:
        tick_started_at = datetime.now(UTC)
        emit("tick_started", started_at=tick_started_at.isoformat())

        engine = open_engine(config.filings_db_path)

        # Pre-tick spend cap check (ADR 0029). The cost_observed events recorded
        # by classify and reduce calls feed daily_cost_usd; the tick refuses to
        # do new LLM-bound work once today's aggregate is at or above the cap.
        # A warn-level aggregate fires a structured event without blocking, so
        # the operator sees the approach before the wall.
        today_utc = datetime.now(UTC).date().isoformat()
        spend_today = daily_cost_usd(engine, today_utc)
        if spend_today >= config.anthropic_daily_cost_cap_usd:
            emit(
                "tick_failed",
                error_class="cost_cap_exceeded",
                daily_spend_usd=round(spend_today, 6),
                cap_usd=config.anthropic_daily_cost_cap_usd,
                day_utc=today_utc,
            )
            sys.exit(1)
        if spend_today >= config.anthropic_daily_cost_warn_usd:
            emit(
                "cost_warning",
                daily_spend_usd=round(spend_today, 6),
                warn_usd=config.anthropic_daily_cost_warn_usd,
                cap_usd=config.anthropic_daily_cost_cap_usd,
                day_utc=today_utc,
            )

        # Route classify- and reduce-stage cost observations through the DB sink
        # so they accumulate for the next tick's pre-check (ADR 0029).
        set_cost_sink(db_cost_sink(engine))

        cursor = read_ingest_cursor(engine)
        cursor_acc = cursor[0] if cursor else None
        cursor_filed = cursor[1] if cursor else None

        target_dates = _dates_to_scan(cursor_filed, today_et=datetime.now(_EASTERN).date())

        new_filings_count = 0
        errors_count = 0
        reduce_errors_count = 0

        with EdgarClient(
            user_agent=config.edgar_user_agent,
            rate_limit_per_second=_EDGAR_RATE_LIMIT_PER_SEC,
        ) as client:
            for target in target_dates:
                try:
                    index_text = fetch_daily_index(target, client)
                except httpx.HTTPStatusError as exc:
                    # EDGAR returns 403 (not 404) for missing daily-index files:
                    # non-business days, future dates, and today before the
                    # ~10 PM ET publish batch. Treat both as skip-and-continue
                    # so the timer keeps polling until the index lands.
                    if exc.response.status_code in (403, 404):
                        emit(
                            "tick_skipped_date",
                            date=target.isoformat(),
                            status=exc.response.status_code,
                            reason="daily index missing (EDGAR 403 for unpublished/non-business)",
                        )
                        continue
                    _fail(
                        tick_started_at,
                        error_class=type(exc).__name__,
                        message=f"HTTP {exc.response.status_code} fetching {target.isoformat()}",
                    )
                    return

                entries = filter_form(parse_daily_index(index_text), "8-K")
                new_entries = _filter_new_entries(engine, entries)

                for entry in new_entries:
                    try:
                        reduce_errors_count += _process_one(client, engine, entry)
                        new_filings_count += 1
                    except Exception as exc:
                        errors_count += 1
                        _fail(
                            tick_started_at,
                            error_class=type(exc).__name__,
                            message=str(exc),
                            accession_number=entry.accession_number,
                            new_filings_count=new_filings_count,
                            errors_count=errors_count,
                        )
                        return

        duration_ms = int((datetime.now(UTC) - tick_started_at).total_seconds() * 1000)
        span.set_attribute("dates_scanned", [d.isoformat() for d in target_dates])
        span.set_attribute("new_filings_count", new_filings_count)
        span.set_attribute("errors_count", errors_count)
        span.set_attribute("reduce_errors_count", reduce_errors_count)
        emit(
            "tick_completed",
            duration_ms=duration_ms,
            new_filings_count=new_filings_count,
            errors_count=errors_count,
            reduce_errors_count=reduce_errors_count,
            dates_scanned=[d.isoformat() for d in target_dates],
            cursor_after=(cursor_acc, cursor_filed) if cursor else None,
        )


def _process_one(client: EdgarClient, engine: Engine, entry: DailyIndexEntry) -> int:
    """Resolve, fetch body, classify-with-retry, persist, reduce, advance cursor.

    Returns the number of reduce failures for this filing (0 or 1). A classify
    or fetch failure raises and fails the tick; a reduce failure does not (see
    `_reduce_one`), so the count is surfaced rather than propagated.
    """
    emit(
        "filing_fetched",
        accession_number=entry.accession_number,
        cik=entry.cik,
        form=entry.form,
        filed_at=entry.filed_at,
        company_name=entry.company_name,
    )
    filing = resolve_filing(entry, client)
    # Populate the ticker from the local CIK→ticker mirror before persisting.
    # Returns the filing unchanged if cik_tickers has no entry — common for
    # private subsidiaries, trusts, or fresh installs before scan-tickers
    # has been run. See ADR 0025.
    ticker = lookup_ticker_by_cik(engine, filing.cik)
    if ticker is not None:
        filing = filing.model_copy(update={"ticker": ticker})
    document = fetch_filing_document(filing, client)
    upsert_filing_document(engine, document)

    emit(
        "classification_started",
        accession_number=entry.accession_number,
        cik=entry.cik,
        items_count=len(document.items),
    )
    result = with_retries(
        lambda: classify_filing(document),
        log_context={
            "accession_number": entry.accession_number,
            "cik": entry.cik,
        },
    )
    inserted = insert_classifications(engine, result)

    emit(
        "classification_completed",
        accession_number=entry.accession_number,
        cik=entry.cik,
        classifications_inserted=inserted,
        classifier_version=result.classifier_version,
        taxonomy_version=result.taxonomy_version,
    )

    reduce_errors = _reduce_one(engine, result)

    advance_ingest_cursor(engine, entry.accession_number, entry.filed_at)
    emit(
        "cursor_advanced",
        accession_number=entry.accession_number,
        filed_at=entry.filed_at,
    )
    return reduce_errors


def _reduce_one(engine: Engine, classification: FilingClassification) -> int:
    """Reduce a freshly-classified filing into events as its own run (ADR 0028).

    Best-effort and non-fatal: the classification — the irreplaceable map output
    — is already persisted and the cursor will advance regardless. Reduce is a
    derived, replayable stage, so a failure here is logged and counted, not
    propagated; a later `reduce-corpus` sweep closes the resulting gap in the
    events layer. Retries cover transient Anthropic errors, as classify does.
    Returns 1 if the reduce failed, 0 otherwise.
    """
    run_id = create_run(
        engine,
        stage="reduce",
        config_version=reducer_version(),
        taxonomy_version=classification.taxonomy_version,
    )
    try:
        events = with_retries(
            lambda: reduce_filing(classification),
            log_context={
                "accession_number": classification.accession_number,
                "cik": classification.cik,
                "stage": "reduce",
            },
        )
        written = insert_events(engine, events, run_id=run_id)
    except Exception as exc:
        complete_run(engine, run_id, status="failed")
        emit(
            "reduce_failed",
            accession_number=classification.accession_number,
            cik=classification.cik,
            run_id=run_id,
            error_class=type(exc).__name__,
            message=str(exc),
        )
        return 1

    complete_run(engine, run_id, status="succeeded")
    emit(
        "reduce_completed",
        accession_number=classification.accession_number,
        cik=classification.cik,
        run_id=run_id,
        events=written,
    )
    return 0


def _dates_to_scan(cursor_filed_at: str | None, today_et: date) -> list[date]:
    """Compute the list of business dates to fetch this tick.

    First-ever tick (cursor unset): scan today only — the first cursor
    advance is "now" per ADR 0021, no backfill.

    Subsequent ticks: scan from cursor's date through today (inclusive),
    covering the cross-midnight ET case where a tick straddles two
    business days.
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


def _parse_filed_at_to_date(value: str) -> date:
    s = value.strip()
    if len(s) == 8 and s.isdigit():
        return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
    return date.fromisoformat(s)


def _filter_new_entries(engine: Engine, entries: list[DailyIndexEntry]) -> list[DailyIndexEntry]:
    """Drop entries whose accession_number is already persisted.

    Correctness here is from the filings PK, not the cursor. One indexed
    lookup per tick instead of per entry; the IN-list scales to peak day
    volume comfortably.
    """
    if not entries:
        return []
    accessions = [e.accession_number for e in entries]
    sql = text("SELECT accession_number FROM filings WHERE accession_number IN :accs").bindparams(
        bindparam("accs", expanding=True)
    )
    with engine.begin() as conn:
        rows = conn.execute(sql, {"accs": accessions}).fetchall()
    seen = {row[0] for row in rows}
    new = [e for e in entries if e.accession_number not in seen]
    new.sort(key=lambda e: (e.filed_at, e.accession_number))
    return new


def _fail(tick_started_at: datetime, **fields: object) -> None:
    duration_ms = int((datetime.now(UTC) - tick_started_at).total_seconds() * 1000)
    emit("tick_failed", duration_ms=duration_ms, **fields)
    sys.exit(1)


if __name__ == "__main__":
    main()
