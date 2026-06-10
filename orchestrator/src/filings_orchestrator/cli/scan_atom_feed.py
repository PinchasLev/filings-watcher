"""CLI: one Atom-feed ingest tick (ADR 0029).

Polls EDGAR's `getcurrent` 8-K Atom snapshot, dedups against the local
filings PK, and classifies+reduces each new entry through the shared
per-filing pipeline. Designed to run on a 30-second `OnUnitInactiveSec`
timer; the spend cap from ADR 0029 gates the tick before any LLM-bound
work begins.

Differences from `scan-daily-index`:

- No cursor. Idempotency on the filings PK is the durability mechanism;
  the Atom snapshot is forward-only in practice and dedup-and-exit is free.
- One HTTP GET per tick — the snapshot itself. No per-date iteration.
- A populated feed is always expected; an empty parse is logged but not
  treated as an error (EDGAR transient or a parser regression would
  surface as zero new filings, which the backstop daily-index path
  reconciles per ADR 0029).

Run as a one-shot under the systemd timer. Output is JSON-line structured
events to stdout, captured by journald.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

from opentelemetry import trace

from filings_orchestrator.cli._pipeline import process_one
from filings_orchestrator.config import MissingConfigError, load_config
from filings_orchestrator.cost import db_llm_call_sink, set_cost_sink
from filings_orchestrator.edgar import EdgarClient
from filings_orchestrator.edgar.atom_feed import (
    fetch_atom_feed,
    filter_form,
    parse_atom_feed,
)
from filings_orchestrator.log_events import emit
from filings_orchestrator.otel_setup import setup_otel
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import (
    daily_cost_usd,
    select_seen_accessions,
)

# Shared EDGAR rate limit. Matches the daily-index CLI's setting; the limiter
# is per-process (one EdgarClient per tick), so each CLI's setting only
# governs its own tick — they don't interfere. See ADR 0021 + ADR 0012.
_EDGAR_RATE_LIMIT_PER_SEC = 2

# Atom snapshot size. 100 entries against a 30s tick is the ADR 0029
# starting tunable — comfortable headroom against the rate at which entries
# roll off the snapshot tail at observed 8-K volume.
_ATOM_FEED_COUNT = 100


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
        emit("tick_started", started_at=tick_started_at.isoformat(), source="atom_feed")

        engine = open_engine(config.filings_db_path)

        # Pre-tick spend cap check (ADR 0029). The latency reduction of the
        # Atom path makes the cap deploy-gating, not eventual — a runaway
        # under 30s ticks burns credit fast.
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

        set_cost_sink(db_llm_call_sink(engine))

        new_filings_count = 0
        errors_count = 0
        reduce_errors_count = 0

        with EdgarClient(
            user_agent=config.edgar_user_agent,
            rate_limit_per_second=_EDGAR_RATE_LIMIT_PER_SEC,
        ) as client:
            atom_xml = fetch_atom_feed(client, form="8-K", count=_ATOM_FEED_COUNT)
            entries = filter_form(parse_atom_feed(atom_xml), "8-K")
            seen = select_seen_accessions(engine, [e.accession_number for e in entries])
            new_entries = sorted(
                (e for e in entries if e.accession_number not in seen),
                key=lambda e: e.updated_at,
            )

            emit(
                "atom_feed_polled",
                entries_total=len(entries),
                entries_new=len(new_entries),
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
                        filed_at=entry.updated_at,
                        submitted_at=entry.updated_at,
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

        duration_ms = int((datetime.now(UTC) - tick_started_at).total_seconds() * 1000)
        span.set_attribute("source", "atom_feed")
        span.set_attribute("new_filings_count", new_filings_count)
        span.set_attribute("errors_count", errors_count)
        span.set_attribute("reduce_errors_count", reduce_errors_count)
        emit(
            "tick_completed",
            source="atom_feed",
            duration_ms=duration_ms,
            new_filings_count=new_filings_count,
            errors_count=errors_count,
            reduce_errors_count=reduce_errors_count,
        )


def _fail(tick_started_at: datetime, **fields: object) -> None:
    duration_ms = int((datetime.now(UTC) - tick_started_at).total_seconds() * 1000)
    emit("tick_failed", source="atom_feed", duration_ms=duration_ms, **fields)
    sys.exit(1)


if __name__ == "__main__":
    main()
