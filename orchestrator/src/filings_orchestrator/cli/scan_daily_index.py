"""CLI: one daily-index ingest tick.

Reads the ingest cursor, fetches each EDGAR daily-index file between the
cursor's date (exclusive) and today (Eastern Time, inclusive), filters
to 8-Ks not yet present in the local DB, classifies each one with
Anthropic-side retries, and advances the cursor per filing.

Per ADR 0021:
- Correctness on dedup comes from the accession-number primary key on
  filings, not the cursor.
- The cursor advances only past filings whose classification persisted.
- On exhausted retries for any filing, the tick logs `tick_failed` and
  exits non-zero; the next tick re-attempts from the failing filing.

Run as a one-shot under the systemd timer (per ADR 0012). Output is
JSON-line structured events to stdout, captured by journald.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import Engine, bindparam, text

from filings_orchestrator.classify import classify_filing
from filings_orchestrator.classify.retry import with_retries
from filings_orchestrator.config import MissingConfigError, load_config
from filings_orchestrator.edgar import EdgarClient, fetch_filing_document
from filings_orchestrator.edgar.daily_index import (
    DailyIndexEntry,
    fetch_daily_index,
    filter_form,
    parse_daily_index,
    resolve_filing,
)
from filings_orchestrator.log_events import emit
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import (
    advance_ingest_cursor,
    insert_classifications,
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
    try:
        config = load_config()
    except MissingConfigError as e:
        emit("tick_failed", error_class="MissingConfigError", message=str(e))
        sys.exit(2)

    os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    os.environ["LANGSMITH_API_KEY"] = config.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = config.langsmith_project
    os.environ["LANGSMITH_TRACING"] = "true" if config.langsmith_tracing else "false"

    tick_started_at = datetime.now(UTC)
    emit("tick_started", started_at=tick_started_at.isoformat())

    engine = open_engine(config.filings_db_path)
    cursor = read_ingest_cursor(engine)
    cursor_acc = cursor[0] if cursor else None
    cursor_filed = cursor[1] if cursor else None

    target_dates = _dates_to_scan(cursor_filed, today_et=datetime.now(_EASTERN).date())

    new_filings_count = 0
    errors_count = 0

    with EdgarClient(
        user_agent=config.edgar_user_agent,
        rate_limit_per_second=_EDGAR_RATE_LIMIT_PER_SEC,
    ) as client:
        for target in target_dates:
            try:
                index_text = fetch_daily_index(target, client)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    emit(
                        "tick_skipped_date",
                        date=target.isoformat(),
                        reason="daily-index not published (likely non-business day)",
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
                    _process_one(client, engine, entry)
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
    emit(
        "tick_completed",
        duration_ms=duration_ms,
        new_filings_count=new_filings_count,
        errors_count=errors_count,
        dates_scanned=[d.isoformat() for d in target_dates],
        cursor_after=(cursor_acc, cursor_filed) if cursor else None,
    )


def _process_one(client: EdgarClient, engine: Engine, entry: DailyIndexEntry) -> None:
    """Resolve, fetch body, classify-with-retry, persist, advance cursor."""
    emit(
        "filing_fetched",
        accession_number=entry.accession_number,
        cik=entry.cik,
        form=entry.form,
        filed_at=entry.filed_at,
        company_name=entry.company_name,
    )
    filing = resolve_filing(entry, client)
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

    advance_ingest_cursor(engine, entry.accession_number, entry.filed_at)
    emit(
        "cursor_advanced",
        accession_number=entry.accession_number,
        filed_at=entry.filed_at,
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
