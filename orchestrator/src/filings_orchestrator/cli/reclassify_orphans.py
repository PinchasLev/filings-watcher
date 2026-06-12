"""CLI: reclassify orphaned filings — the classify-layer reconciler (ADR 0030).

An orphan is a `filings` row with zero classifications: the map stage failed
after the filing row was persisted, and because ingest dedup keys on
row-existence rather than on whether classification completed, every later tick
skips it forever. This reconciler is the classify-layer counterpart to
`reduce-corpus`: it selects those filings and re-runs the map stage over their
**stored body text** — no EDGAR re-fetch, since filing text is immutable
(ADR 0028/0030) — then reduces each into the events layer via the same
`classify_and_reduce` tail the live ingest path uses.

    uv run reclassify-orphans --dry-run    # list/count orphans, no LLM calls
    uv run reclassify-orphans              # heal: classify + reduce each orphan

Cost-cap gated: before each filing it checks today's spend against
`ANTHROPIC_DAILY_COST_CAP_USD` and stops cleanly once reached, so a large
backlog heals across days rather than blowing the budget. Re-runnable and
idempotent — a healed filing drops out of the orphan set, so a later run only
sees what still needs work. Continue-on-failure: one filing that fails
classification does not abort the rest of the backlog.

Needs the Anthropic credential and DB path; no EDGAR user agent (no fetch).
`--dry-run` needs only the DB path. Output is JSON-line structured events to
stdout; exits non-zero if `--dry-run` finds orphans or if any heal fails.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

from filings_orchestrator.cli._pipeline import classify_and_reduce
from filings_orchestrator.config import (
    MissingConfigError,
    get_config_float,
    get_config_str,
    get_secret,
)
from filings_orchestrator.cost import db_llm_call_sink, set_cost_sink
from filings_orchestrator.log_events import emit
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import (
    daily_cost_usd,
    list_orphaned_accessions,
    load_filing_document,
)

_DEFAULT_DAILY_COST_CAP_USD = 5.00


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="reclassify-orphans",
        description="Re-run classification for filings that have no classifications.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List and count orphans without classifying them (no LLM calls).",
    )
    args = parser.parse_args()

    db_path = get_config_str("FILINGS_DB_PATH", default="/var/lib/filings-watcher/filings.db")
    engine = open_engine(db_path)

    orphans = list_orphaned_accessions(engine)
    emit("reclassify_orphans_started", orphans=len(orphans), dry_run=args.dry_run)

    if args.dry_run:
        for accession in orphans:
            emit("orphan_found", accession=accession)
        emit(
            "reclassify_orphans_completed",
            reclassified=0,
            failed=0,
            skipped=0,
            stopped_at_cap=False,
            remaining=len(orphans),
        )
        if orphans:
            sys.exit(1)
        return

    # The heal path classifies, so it needs the Anthropic credential. ChatAnthropic
    # reads it from the environment; mirror the other CLIs and set it explicitly.
    try:
        anthropic_key = get_secret("ANTHROPIC_API_KEY")
    except MissingConfigError as exc:
        emit("reclassify_orphans_failed", error_class="MissingConfigError", message=str(exc))
        sys.exit(2)
    os.environ["ANTHROPIC_API_KEY"] = anthropic_key

    cap_usd = get_config_float("ANTHROPIC_DAILY_COST_CAP_USD", _DEFAULT_DAILY_COST_CAP_USD)
    # Route classify/reduce LLM-call observations through the DB sink so the
    # per-call rows accumulate into the daily aggregate this loop consults.
    set_cost_sink(db_llm_call_sink(engine))

    reclassified = 0
    failed = 0
    skipped = 0
    stopped_at_cap = False

    for accession in orphans:
        spend_today = daily_cost_usd(engine, datetime.now(UTC).date().isoformat())
        if spend_today >= cap_usd:
            emit(
                "reclassify_stopped",
                reason="cost_cap_reached",
                daily_spend_usd=round(spend_today, 6),
                cap_usd=cap_usd,
                reclassified=reclassified,
            )
            stopped_at_cap = True
            break

        document = load_filing_document(engine, accession)
        if document is None:
            emit("reclassify_skipped", accession=accession, reason="no_stored_body")
            skipped += 1
            continue

        try:
            reduce_failed = classify_and_reduce(engine, document)
        except Exception as exc:  # heal the rest of the backlog; surface this one
            failed += 1
            emit(
                "reclassify_failed",
                accession=accession,
                error_class=type(exc).__name__,
                message=str(exc),
            )
            continue

        reclassified += 1
        emit("reclassify_completed", accession=accession, reduce_failed=reduce_failed)

    emit(
        "reclassify_orphans_completed",
        reclassified=reclassified,
        failed=failed,
        skipped=skipped,
        stopped_at_cap=stopped_at_cap,
        remaining=len(orphans) - reclassified,
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
