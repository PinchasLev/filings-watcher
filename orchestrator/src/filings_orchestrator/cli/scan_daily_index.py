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

from filings_orchestrator.alerting import ALERT, emit_alert
from filings_orchestrator.business_days import is_business_day, parse_filed_at_to_date
from filings_orchestrator.cli._pipeline import process_one, verify_taxonomy
from filings_orchestrator.config import MissingConfigError, load_config
from filings_orchestrator.cost import db_llm_call_sink, set_cost_sink
from filings_orchestrator.edgar import EdgarClient
from filings_orchestrator.edgar.daily_index import (
    fetch_daily_index,
    filter_form,
    parse_daily_index,
)
from filings_orchestrator.log_events import emit
from filings_orchestrator.otel_setup import setup_otel
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import (
    advance_ingest_cursor,
    daily_cost_usd,
    read_ingest_cursor,
    select_seen_accessions,
)

# EDGAR rate limit for the unattended ingest path. The published ceiling is
# 10 req/sec; 2 req/sec leaves significant headroom for retries and for the
# operator-on-demand `classify-filing` to share the limiter without
# interference. See ADR 0021 + ADR 0012.
_EDGAR_RATE_LIMIT_PER_SEC = 2

_EASTERN = ZoneInfo("America/New_York")

# Forms ingested from the daily index. 8-K (domestic current reports) and 6-K
# (foreign private issuer reports) share the entire downstream pipeline — resolve,
# fetch, classify, reduce — differing only in how the classifier sections a filing
# (8-K by Item, 6-K by furnished exhibit). Amendments (8-K/A, 6-K/A) are excluded
# by the exact-match filter, consistent with prior behavior. See ADR 0033.
_INGEST_FORMS = ("8-K", "6-K")


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
        verify_taxonomy(engine)

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
            # Alert (ADR 0031): the cap is a pre-tick gate that exits before any
            # classify call, so it never reaches the classify-failure alert path —
            # hitting it silently halts classification for the rest of the UTC day.
            # Surface it once per day (dedup per date) so the operator knows the
            # product has gone dark and can decide to economize or raise the cap.
            emit_alert(
                engine,
                ALERT,
                "Daily cost cap reached — classification paused",
                body=(
                    f"Today's Anthropic spend (${spend_today:.2f}) reached the daily cap "
                    f"(${config.anthropic_daily_cost_cap_usd:.2f}). New filings will not be "
                    f"classified until the cap resets at 00:00 UTC; the daily-index reconciler "
                    f"backfills the gap. If this fires early in the day, reduce per-filing cost "
                    f"or raise ANTHROPIC_DAILY_COST_CAP_USD."
                ),
                dedup_key=f"cost_cap:{today_utc}",
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

        # Route classify- and reduce-stage LLM-call observations through the DB
        # sink so the per-call rows accumulate for the next tick's pre-check
        # (ADR 0029). Tokens are recorded for engineering analysis; the cost
        # column drives the cap.
        set_cost_sink(db_llm_call_sink(engine))

        cursor = read_ingest_cursor(engine)
        cursor_acc = cursor[0] if cursor else None
        cursor_filed = cursor[1] if cursor else None

        today_et = datetime.now(_EASTERN).date()
        target_dates = _dates_to_scan(cursor_filed, today_et=today_et)

        new_filings_count = 0
        errors_count = 0
        reduce_errors_count = 0
        # ADR 0029: track whether today's daily-index file was attempted and
        # found missing, so the late-cluster invocation can emit a
        # `daily_index_publication_missing` signal at end-of-tick.
        today_publication_missing = False

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
                        if target == today_et:
                            today_publication_missing = True
                        continue
                    _fail(
                        tick_started_at,
                        error_class=type(exc).__name__,
                        message=f"HTTP {exc.response.status_code} fetching {target.isoformat()}",
                    )
                    return

                all_entries = parse_daily_index(index_text)
                entries = [e for form in _INGEST_FORMS for e in filter_form(all_entries, form)]
                if target == today_et:
                    # ADR 0029: heartbeat that EDGAR's daily-index publication
                    # for today has been detected. Multiple cluster invocations
                    # may all emit (each fetches the same file independently);
                    # downstream consumers take MIN(emitted_at) per date as the
                    # canonical publication signal.
                    emit(
                        "daily_index_published",
                        date=target.isoformat(),
                        total_entries=len(all_entries),
                        eight_k_entries=len(filter_form(all_entries, "8-K")),
                        six_k_entries=len(filter_form(all_entries, "6-K")),
                    )
                seen = select_seen_accessions(engine, [e.accession_number for e in entries])
                new_entries = sorted(
                    (e for e in entries if e.accession_number not in seen),
                    key=lambda e: (e.filed_at, e.accession_number),
                )

                for entry in new_entries:
                    try:
                        reduce_errors_count += process_one(
                            client=client,
                            engine=engine,
                            cik=entry.cik,
                            accession_number=entry.accession_number,
                            company_name=entry.company_name,
                            form=entry.form,
                            filed_at=entry.filed_at,
                        )
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

                # This date's index is now fully accounted for — every 8-K is
                # ingested, whether we just processed it or the atom path got there
                # first. Advance the cursor to the date's high-water entry so a day
                # of pure duplicates still moves us forward. Advancing inside the
                # loop above would freeze the cursor whenever atom front-runs the
                # daily index (new_entries empty), leaving _dates_to_scan to
                # re-fetch an ever-growing span every tick.
                if entries:
                    high_water = max(entries, key=lambda e: (e.filed_at, e.accession_number))
                    advance_ingest_cursor(engine, high_water.accession_number, high_water.filed_at)
                    emit(
                        "cursor_advanced",
                        accession_number=high_water.accession_number,
                        filed_at=high_water.filed_at,
                    )

        # ADR 0029: if today's file was missing at or after the cluster's
        # last invocation window (23:00 ET), emit the publication-missing
        # signal so downstream alarming can distinguish "EDGAR slipped"
        # from "our ingest broke". The is_business_day flag lets
        # consumers route weekend dates as informational and business-day
        # misses as alarm-eligible.
        if today_publication_missing and datetime.now(_EASTERN).hour >= 23:
            today_is_business_day = is_business_day(today_et)
            emit(
                "daily_index_publication_missing",
                date=today_et.isoformat(),
                is_business_day=today_is_business_day,
            )
            # Absence alarm (ADR 0031): a business-day miss at the end of the
            # evening cluster means EDGAR slipped its publish OR our ingest is
            # broken — either way filings for today may be missing, which a human
            # should look at. Weekend/holiday misses are expected, so they stay
            # informational (structured log only). dedup_key per date so a single
            # dark day pages once, not on every late-cluster invocation.
            if today_is_business_day:
                emit_alert(
                    engine,
                    ALERT,
                    "Daily index not published",
                    body=(
                        f"EDGAR's daily index for {today_et.isoformat()} was not found by "
                        f"the end of the evening cluster. EDGAR may have slipped its publish, "
                        f"or our ingest is broken — today's filings may be missing. Check the "
                        f"EDGAR full-index site and the ingest logs."
                    ),
                    dedup_key=f"daily_index_missing:{today_et.isoformat()}",
                    date=today_et.isoformat(),
                )

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
    start = parse_filed_at_to_date(cursor_filed_at)
    if start > today_et:
        return [today_et]
    out: list[date] = []
    current = start
    while current <= today_et:
        out.append(current)
        current = date.fromordinal(current.toordinal() + 1)
    return out


def _fail(tick_started_at: datetime, **fields: object) -> None:
    duration_ms = int((datetime.now(UTC) - tick_started_at).total_seconds() * 1000)
    emit("tick_failed", duration_ms=duration_ms, **fields)
    sys.exit(1)


if __name__ == "__main__":
    main()
