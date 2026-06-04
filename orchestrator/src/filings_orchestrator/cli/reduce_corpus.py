"""CLI: reduce stored classifications into filing-level events (ADR 0027/0028).

Replays the reduce stage over already-classified filings, writing the
deduplicated `events` layer. Each filing is reduced as its own run (run_id is
the versioning axis, ADR 0028); this is the first concrete instance of the
corpus-reprocessing capability ADR 0028 describes.

    uv run reduce-corpus --accession 0001922446-26-000004   # one filing
    uv run reduce-corpus                                     # every classified filing

Reduce does not fetch from EDGAR (filing text is immutable and already stored),
so this CLI needs only the Anthropic credential and the DB path — not the
EDGAR user agent the full config requires.

Output is JSON-line structured events to stdout. Exit is non-zero if any
targeted filing failed to reduce.
"""

from __future__ import annotations

import argparse
import os
import sys

from filings_orchestrator.classify import reduce_filing, reducer_version
from filings_orchestrator.config import MissingConfigError, get_config_str, get_secret
from filings_orchestrator.cost import db_cost_sink, set_cost_sink
from filings_orchestrator.log_events import emit
from filings_orchestrator.persistence import open_engine
from filings_orchestrator.persistence.repository import (
    complete_run,
    create_run,
    insert_events,
    list_classified_accessions,
    load_latest_filing_classification,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="reduce-corpus",
        description="Reduce stored per-Item classifications into filing-level events.",
    )
    parser.add_argument(
        "--accession",
        help="Reduce a single filing by accession number (default: every classified filing).",
    )
    args = parser.parse_args()

    try:
        anthropic_key = get_secret("ANTHROPIC_API_KEY")
    except MissingConfigError as e:
        emit("reduce_corpus_failed", error_class="MissingConfigError", message=str(e))
        sys.exit(2)
    # ChatAnthropic reads the key from the environment; ensure it is present
    # even when the source was an .env file rather than a real env var.
    os.environ["ANTHROPIC_API_KEY"] = anthropic_key

    db_path = get_config_str("FILINGS_DB_PATH", default="/var/lib/filings-watcher/filings.db")
    engine = open_engine(db_path)

    # Reduce calls Anthropic per filing; route the cost observations through the
    # DB sink so they contribute to the daily aggregate the live tick's pre-check
    # consults (ADR 0029). This CLI does not enforce the cap itself — it is an
    # operator-invoked sweep, and an in-flight sweep should not interrupt itself
    # mid-corpus; the cost surface records, the next live tick decides.
    set_cost_sink(db_cost_sink(engine))

    if args.accession:
        accessions = [args.accession]
    else:
        accessions = list_classified_accessions(engine)

    config_version = reducer_version()
    emit("reduce_corpus_started", filings=len(accessions), reducer_version=config_version)

    reduced = 0
    events_written = 0
    skipped = 0
    failed = 0

    for accession in accessions:
        classification = load_latest_filing_classification(engine, accession)
        if classification is None:
            emit("reduce_skipped", accession=accession, reason="no_classifications")
            skipped += 1
            continue

        run_id = create_run(
            engine,
            stage="reduce",
            config_version=config_version,
            taxonomy_version=classification.taxonomy_version,
        )
        try:
            events = reduce_filing(classification)
            written = insert_events(engine, events, run_id=run_id)
        except Exception as exc:  # record the failure; keep reducing the rest of the corpus
            complete_run(engine, run_id, status="failed")
            emit(
                "reduce_failed",
                accession=accession,
                run_id=run_id,
                error_class=type(exc).__name__,
                message=str(exc),
            )
            failed += 1
            if args.accession:
                # Single-filing mode: surface the failure to the operator.
                raise
            continue

        complete_run(engine, run_id, status="succeeded")
        reduced += 1
        events_written += written
        emit(
            "reduce_completed",
            accession=accession,
            run_id=run_id,
            events=written,
        )

    emit(
        "reduce_corpus_completed",
        reduced=reduced,
        events_written=events_written,
        skipped=skipped,
        failed=failed,
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
