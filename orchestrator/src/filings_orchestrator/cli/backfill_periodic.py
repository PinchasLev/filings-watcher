"""CLI: backfill periodic (10-K) filings for an explicit set of tickers or CIKs.

A coverage-expansion tool. Unlike scan-periodic (which walks the daily index by date),
this targets *named* companies: resolve each to a CIK, page EDGAR's submissions history
(so firehose filers like JPMorgan still yield their prior-year 10-K, not just the one in
the recent window), fetch each filing's primary document, segment Item 1A into Risk
Factors blocks, and store the envelope + blocks. The embed / diff / judge / synthesize
reconcilers then pick them up.

Deterministic, no LLM, no cost cap. A one-off operator tool — never timered (timering it
would re-fetch the same annual reports every firing). Output is JSON-line events.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from opentelemetry import trace

from filings_orchestrator.change_detection import segment_risk_factors
from filings_orchestrator.config import MissingConfigError, get_config_str, require_env
from filings_orchestrator.edgar import EdgarClient
from filings_orchestrator.edgar.document import fetch_markup_text
from filings_orchestrator.edgar.filings import load_ticker_index, periodic_filings_for_cik
from filings_orchestrator.log_events import emit
from filings_orchestrator.otel_setup import setup_otel
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import insert_periodic_filing

_DEFAULT_FORM = "10-K"
_DEFAULT_LIMIT = 2  # two years -> one year-over-year diff
_DEFAULT_RATE = 8.0  # EDGAR allows 10/sec; leave headroom


def _resolve_targets(
    tickers: list[str], ciks: list[str], index: dict[str, tuple[str, str]]
) -> tuple[list[tuple[str, str]], int]:
    """(cik_padded, name) per requested target, deduped by CIK (order preserved). Returns
    the targets and the count of tickers that did not resolve (reported + skipped)."""
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    unresolved = 0
    for tk in tickers:
        u = tk.upper()
        info = index.get(u) or index.get(u.replace(".", "-")) or index.get(u.replace(".", ""))
        if info is None:
            unresolved += 1
            emit("backfill_ticker_unresolved", ticker=tk)
            continue
        cik, name = info
        if cik not in seen:
            seen.add(cik)
            targets.append((cik, name))
    for raw in ciks:
        cik = f"{int(raw):010d}"
        if cik not in seen:
            seen.add(cik)
            targets.append((cik, cik))
    return targets, unresolved


def main() -> None:
    setup_otel()
    parser = argparse.ArgumentParser(
        prog="backfill-periodic",
        description="Backfill 10-K Risk Factors blocks for named companies (coverage expansion).",
    )
    parser.add_argument("--tickers", default="", help="Comma-separated tickers, e.g. AAPL,MSFT.")
    parser.add_argument(
        "--ciks", default="", help="Comma-separated CIKs (with or without --tickers)."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        help=f"Most-recent filings per company (default {_DEFAULT_LIMIT}; 2 gives one diff).",
    )
    parser.add_argument(
        "--form", default=_DEFAULT_FORM, help=f"Periodic form (default {_DEFAULT_FORM})."
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=_DEFAULT_RATE,
        help=f"EDGAR requests/sec (default {_DEFAULT_RATE}; EDGAR's cap is 10).",
    )
    args = parser.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    ciks = [c.strip() for c in args.ciks.split(",") if c.strip()]
    if not tickers and not ciks:
        emit(
            "tick_failed",
            source="backfill_periodic",
            error_class="NoTargets",
            message="one of --tickers / --ciks is required",
        )
        sys.exit(2)

    try:
        edgar_user_agent = require_env("EDGAR_USER_AGENT")
    except MissingConfigError as e:
        emit(
            "tick_failed",
            source="backfill_periodic",
            error_class="MissingConfigError",
            message=str(e),
        )
        sys.exit(2)
    engine = open_engine(
        get_config_str("FILINGS_DB_PATH", default="/var/lib/filings-watcher/filings.db")
    )
    ingested_at = datetime.now(UTC).isoformat()

    tracer = trace.get_tracer("filings_orchestrator")
    with tracer.start_as_current_span("tick") as span:
        started = datetime.now(UTC)
        emit(
            "tick_started",
            source="backfill_periodic",
            started_at=started.isoformat(),
            form=args.form,
            limit=args.limit,
        )
        stats = {
            "companies": 0,
            "filings": 0,
            "parsed_ok": 0,
            "zero_block": 0,
            "under_limit": 0,
            "unresolved": 0,
            "errors": 0,
        }

        with EdgarClient(user_agent=edgar_user_agent, rate_limit_per_second=args.rate) as client:
            index = load_ticker_index(client) if tickers else {}
            targets, stats["unresolved"] = _resolve_targets(tickers, ciks, index)
            for cik, name in targets:
                stats["companies"] += 1
                try:
                    filings = periodic_filings_for_cik(
                        cik, name, client, form=args.form, limit=args.limit
                    )
                    if len(filings) < args.limit:
                        stats["under_limit"] += 1
                    for f in filings:
                        markup = fetch_markup_text(client, f.primary_document_url)
                        blocks = segment_risk_factors(markup) if markup else []
                        insert_periodic_filing(
                            engine,
                            accession_number=f.accession_number,
                            cik=cik,
                            company_name=f.company_name,
                            form=f.form,
                            filed_at=f.filing_date.isoformat(),
                            period_of_report=f.report_date.isoformat() if f.report_date else None,
                            fiscal_year=f.report_date.year if f.report_date else None,
                            parsed=markup is not None,
                            blocks=blocks,
                            ingested_at=ingested_at,
                        )
                        stats["filings"] += 1
                        stats["parsed_ok" if blocks else "zero_block"] += 1
                        emit(
                            "backfill_filing",
                            cik=cik,
                            accession_number=f.accession_number,
                            block_count=len(blocks),
                        )
                except Exception as exc:
                    stats["errors"] += 1
                    emit(
                        "backfill_error", cik=cik, error_class=type(exc).__name__, message=str(exc)
                    )

        span.set_attribute("source", "backfill_periodic")
        span.set_attribute("companies", stats["companies"])
        emit(
            "tick_completed",
            source="backfill_periodic",
            duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
            **stats,
        )


if __name__ == "__main__":
    main()
