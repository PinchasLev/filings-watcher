"""CLI: exhibit-ab — offline A/B evaluation of EX-99 exhibit ingestion (ADR 0031).

Measures the causal effect of feeding EX-99 exhibit text to the classifier
(shipped in #110): for a sample of exhibit-bearing filings, classify each one
TWICE — once WITH the exhibits (current behavior) and once WITHOUT (body-only,
the prior behavior) — and diff the results. Isolates the exhibit's effect because
both runs share an identical input except the exhibit toggle.

    uv run exhibit-ab                 # evaluate all exhibit-bearing filings
    uv run exhibit-ab --limit 50      # bound the sample (doubles LLM calls/filing)
    uv run exhibit-ab --accession X   # one filing, for a spot check

This is OFFLINE EVALUATION, not product A/B: no traffic splitting, just a sample
classified two ways and diffed. Per-filing diffs and an aggregate summary go to
stdout as JSON lines (ephemeral — not persisted). Sample is unbiased (all
exhibit-bearing filings, NOT pre-filtered by item); segment by item in analysis
of the output. Only filings ingested after #110 carry stored `exhibits_json`, so
run it once exhibit-bearing filings have accumulated.

The harness reconstructs each document from stored data (no EDGAR re-fetch). It
doubles classify cost on the sample, so it is cost-cap gated (ADR 0029) and stops
cleanly at the cap; `--limit` bounds spend. One-off operator tool — never timered.

The diff/aggregate core lives in `cli/_eval.py` (baseline vs candidate) and is
shared with `classify-ab`; only the with/without-exhibit toggle is specific here.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from functools import partial

from filings_orchestrator.classify import classify_filing
from filings_orchestrator.classify.retry import with_retries
from filings_orchestrator.cli._eval import diff_filing, summarize
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
    list_exhibit_bearing_accessions,
    load_filing_document,
)

_DEFAULT_DAILY_COST_CAP_USD = 5.00


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="exhibit-ab",
        description="A/B evaluate EX-99 exhibit ingestion: classify each filing with and without "
        "exhibits and diff the results.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max filings to evaluate.")
    parser.add_argument("--accession", default=None, help="Evaluate a single accession number.")
    args = parser.parse_args()

    db_path = get_config_str("FILINGS_DB_PATH", default="/var/lib/filings-watcher/filings.db")
    engine = open_engine(db_path)

    if args.accession:
        sample = [args.accession]
    else:
        sample = list_exhibit_bearing_accessions(engine, limit=args.limit)

    emit("exhibit_ab_started", sample_size=len(sample))
    if not sample:
        emit("exhibit_ab_completed", filings=0, note="no exhibit-bearing filings found")
        return

    # Classifying needs the Anthropic credential; route call costs through the DB
    # sink so the cost cap (ADR 0029) sees this run's spend.
    try:
        os.environ["ANTHROPIC_API_KEY"] = get_secret("ANTHROPIC_API_KEY")
    except MissingConfigError as exc:
        emit("exhibit_ab_failed", error_class="MissingConfigError", message=str(exc))
        sys.exit(2)
    cap_usd = get_config_float("ANTHROPIC_DAILY_COST_CAP_USD", _DEFAULT_DAILY_COST_CAP_USD)
    set_cost_sink(db_llm_call_sink(engine))

    results: list[dict[str, object]] = []
    skipped = 0
    stopped_at_cap = False
    for accession in sample:
        if daily_cost_usd(engine, datetime.now(UTC).date().isoformat()) >= cap_usd:
            emit("exhibit_ab_stopped", reason="cost_cap_reached", evaluated=len(results))
            stopped_at_cap = True
            break

        document = load_filing_document(engine, accession)
        if document is None or not document.exhibits:
            emit("exhibit_ab_skipped", accession=accession, reason="no_document_or_exhibits")
            skipped += 1
            continue

        without = document.model_copy(update={"exhibits": []})
        try:
            with_ex = with_retries(partial(classify_filing, document))
            without_ex = with_retries(partial(classify_filing, without))
        except Exception as exc:  # keep going; report the failure for this one
            emit(
                "exhibit_ab_skipped",
                accession=accession,
                reason="classify_failed",
                error_class=type(exc).__name__,
            )
            skipped += 1
            continue

        # Baseline = body-only (the prior behavior); candidate = with exhibits.
        diff = diff_filing(accession, baseline=without_ex, candidate=with_ex)
        results.append(diff)
        emit("exhibit_ab_result", **diff)

    emit(
        "exhibit_ab_summary",
        skipped=skipped,
        stopped_at_cap=stopped_at_cap,
        **summarize(results),
    )


if __name__ == "__main__":
    main()
