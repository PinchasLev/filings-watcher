"""CLI: diff each filing's risk factors against the prior period (ADR 0042, PR 4).

A resumable reconciler: finds filings that are fully embedded for the configured
model, have a prior comparable filing (same company, next-earlier parsed period)
that is also embedded, and have no diff yet — then aligns the two filings' block
vectors, classifies each block added/changed/carried/dropped, and stores the
shortlist. Deterministic given the embeddings — **no LLM** (materiality judgment is
the next PR).

Idempotent and gap-driven (no cursor): a filing whose prior is not embedded yet is
skipped and retried next run; a true first filing (no earlier parsed period) is
excluded until an earlier filing is backfilled. Bounded per run via --limit.

Run as a one-shot (a systemd timer wiring is a separate infra step). Output is
JSON-line events to stdout.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy import Engine

from filings_orchestrator.change_detection import DEFAULT_MODEL, diff_blocks
from filings_orchestrator.config import get_config_int, get_config_str
from filings_orchestrator.log_events import emit
from filings_orchestrator.otel_setup import setup_otel
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import (
    find_prior_periodic_filing,
    insert_filing_diff,
    is_filing_fully_embedded,
    load_block_vectors,
    select_filings_needing_diff,
)

# Section scope for this arc — Risk Factors only (ADR 0042).
_SECTION = "risk_factors"
# Diffs computed per run; a backlog drains across runs. A backfill raises it.
_DEFAULT_MAX_PER_RUN = 500


def diff_pass(engine: Engine, *, section: str, model_id: str, limit: int) -> dict[str, int]:
    """Compute and store diffs for up to `limit` ready filings. Returns per-run
    counts. A candidate whose prior is not embedded yet is skipped (retried next
    run), not failed."""
    computed = 0
    skipped_prior_pending = 0
    candidates = select_filings_needing_diff(engine, section, model_id, limit)
    for cand in candidates:
        prior = find_prior_periodic_filing(
            engine, cik=cand.cik, form=cand.form, before_period=cand.period_of_report
        )
        if prior is None:
            # select_... guarantees an earlier parsed filing exists, so this is only
            # reachable on a race; treat as not-ready and move on.
            skipped_prior_pending += 1
            continue
        if not is_filing_fully_embedded(engine, prior, section, model_id):
            skipped_prior_pending += 1
            continue

        current_vectors = load_block_vectors(engine, cand.accession_number, section, model_id)
        prior_vectors = load_block_vectors(engine, prior, section, model_id)
        result = diff_blocks(current_vectors, prior_vectors)
        insert_filing_diff(
            engine,
            accession_number=cand.accession_number,
            prior_accession_number=prior,
            section=section,
            model_id=model_id,
            result=result,
            computed_at=datetime.now(UTC).isoformat(),
        )
        emit(
            "filing_diffed",
            accession_number=cand.accession_number,
            prior_accession_number=prior,
            added=result.added,
            changed=result.changed,
            carried=result.carried,
            dropped=result.dropped,
        )
        computed += 1
    return {
        "computed": computed,
        "skipped_prior_pending": skipped_prior_pending,
        "candidates": len(candidates),
    }


def main() -> None:
    setup_otel()
    parser = argparse.ArgumentParser(
        prog="diff-filings",
        description="Diff each filing's risk factors against the prior period.",
    )
    parser.add_argument(
        "--model",
        help=f"Embedding model id to diff on (default: env VOYAGE_MODEL or {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Max filings to diff this run (default: env MAX_DIFF_FILINGS_PER_RUN or "
        f"{_DEFAULT_MAX_PER_RUN}); raise for a one-off backfill.",
    )
    args = parser.parse_args()

    db_path = get_config_str("FILINGS_DB_PATH", default="/var/lib/filings-watcher/filings.db")
    model = args.model or get_config_str("VOYAGE_MODEL", default=DEFAULT_MODEL)
    limit = args.limit or get_config_int("MAX_DIFF_FILINGS_PER_RUN", _DEFAULT_MAX_PER_RUN)
    engine = open_engine(db_path)

    tracer = trace.get_tracer("filings_orchestrator")
    with tracer.start_as_current_span("tick") as span:
        started = datetime.now(UTC)
        emit(
            "tick_started",
            source="diff",
            started_at=started.isoformat(),
            model=model,
            limit=limit,
        )
        counts = diff_pass(engine, section=_SECTION, model_id=model, limit=limit)
        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        span.set_attribute("source", "diff")
        span.set_attribute("computed", counts["computed"])
        emit("tick_completed", source="diff", duration_ms=duration_ms, model=model, **counts)


if __name__ == "__main__":
    main()
