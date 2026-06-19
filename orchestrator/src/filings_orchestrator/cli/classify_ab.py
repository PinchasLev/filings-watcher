"""CLI: classify-ab — offline A/B evaluation of a taxonomy change (ADR 0032).

Measures the effect of a taxonomy change before it ships (the §7 A/B gate): for a
sample of filings, classify each one under two choice-sets — a **baseline**
(a prior `taxonomy_version`, reconstructed from its stored snapshot) and a
**candidate** (the current in-code taxonomy) — and diff the results. Both arms use
the same model and the same documents; only the offered leaf-set differs, so the
difference is the taxonomy change's effect.

    uv run classify-ab --baseline-version v1               # all classified filings
    uv run classify-ab --baseline-version v1 --limit 50    # bound the sample
    uv run classify-ab --baseline-version v1 --accession X # one filing

This is the second use of the offline-eval core (`cli/_eval.py`), shared with
exhibit-ab. Per-filing diffs and an aggregate summary go to stdout as JSON lines
(ephemeral). The candidate is the in-code taxonomy, so run this on the branch that
adds the change. Documents are reconstructed from stored data (no EDGAR refetch).
Cost-cap gated (ADR 0029); it classifies each filing twice, so `--limit` bounds
spend. One-off operator tool — never timered.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from functools import partial

from filings_orchestrator.classify import FilingClassification, classify_filing
from filings_orchestrator.classify.retry import with_retries
from filings_orchestrator.classify.taxonomy import EventType
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
    list_classified_accessions,
    load_filing_document,
)
from filings_orchestrator.persistence.taxonomy_snapshot import leaves_for_version

_DEFAULT_DAILY_COST_CAP_USD = 5.00


def _baseline_leaves(engine: object, version: str) -> list[EventType]:
    """The baseline version's leaf-set as EventType members, in declaration order.

    Filters the in-code `EventType` to the leaves the snapshot recorded for
    `version`, so the prompt order matches that version's. Leaves no longer in the
    code (only possible after a breaking change) are skipped; this harness targets
    additive changes, where the baseline is a subset of the current taxonomy.
    """
    recorded = set(leaves_for_version(engine, version))  # type: ignore[arg-type]
    return [event_type for event_type in EventType if event_type.value in recorded]


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="classify-ab",
        description="A/B a taxonomy change: classify a sample under a prior version (baseline) "
        "and the in-code taxonomy (candidate) and diff.",
    )
    parser.add_argument(
        "--baseline-version",
        required=True,
        help="The prior taxonomy_version to use as the baseline arm (from its snapshot).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max filings to evaluate.")
    parser.add_argument("--accession", default=None, help="Evaluate a single accession number.")
    args = parser.parse_args()

    db_path = get_config_str("FILINGS_DB_PATH", default="/var/lib/filings-watcher/filings.db")
    engine = open_engine(db_path)

    baseline_leaves = _baseline_leaves(engine, args.baseline_version)
    if not baseline_leaves:
        emit(
            "classify_ab_failed",
            reason="baseline_version_not_snapshotted",
            baseline_version=args.baseline_version,
        )
        sys.exit(2)

    if args.accession:
        sample = [args.accession]
    else:
        sample = list_classified_accessions(engine)
        if args.limit is not None:
            sample = sample[: args.limit]

    emit(
        "classify_ab_started",
        baseline_version=args.baseline_version,
        baseline_leaf_count=len(baseline_leaves),
        candidate_leaf_count=len(list(EventType)),
        sample_size=len(sample),
    )
    if not sample:
        emit("classify_ab_completed", filings=0, note="no classified filings found")
        return

    try:
        os.environ["ANTHROPIC_API_KEY"] = get_secret("ANTHROPIC_API_KEY")
    except MissingConfigError as exc:
        emit("classify_ab_failed", error_class="MissingConfigError", message=str(exc))
        sys.exit(2)
    cap_usd = get_config_float("ANTHROPIC_DAILY_COST_CAP_USD", _DEFAULT_DAILY_COST_CAP_USD)
    set_cost_sink(db_llm_call_sink(engine))

    results: list[dict[str, object]] = []
    skipped = 0
    stopped_at_cap = False
    for accession in sample:
        if daily_cost_usd(engine, datetime.now(UTC).date().isoformat()) >= cap_usd:
            emit("classify_ab_stopped", reason="cost_cap_reached", evaluated=len(results))
            stopped_at_cap = True
            break

        document = load_filing_document(engine, accession)
        if document is None:
            emit("classify_ab_skipped", accession=accession, reason="no_stored_body")
            skipped += 1
            continue

        try:
            # Baseline = the prior version's leaf-set; candidate = in-code (None).
            baseline: FilingClassification = with_retries(
                partial(classify_filing, document, leaves=baseline_leaves)
            )
            candidate: FilingClassification = with_retries(partial(classify_filing, document))
        except Exception as exc:  # keep going; report this one
            emit(
                "classify_ab_skipped",
                accession=accession,
                reason="classify_failed",
                error_class=type(exc).__name__,
            )
            skipped += 1
            continue

        diff = diff_filing(accession, baseline=baseline, candidate=candidate)
        results.append(diff)
        emit("classify_ab_result", **diff)

    emit(
        "classify_ab_summary",
        baseline_version=args.baseline_version,
        skipped=skipped,
        stopped_at_cap=stopped_at_cap,
        **summarize(results),
    )


if __name__ == "__main__":
    main()
