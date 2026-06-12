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
    uv run reclassify-orphans --force      # also retry the abandoned dead-letter

**Dead-letter (ADR 0030).** Most orphans come from transient or credit failures
and heal on the next attempt. A few may fail deterministically — the model
completes but its output fails our schema, re-burning tokens on every retry.
Each such *non-transient* failure bumps the filing's `classify_attempts`; once it
reaches `_MAX_CLASSIFY_ATTEMPTS` the filing is **abandoned** (surfaced via a
`classification_abandoned` event) and dropped from the normal work set, so
repeated runs do not re-charge poison records. `--force` re-includes the
abandoned set — e.g. after a model/prompt change, or an auth/credit fix that was
mis-counted as a content failure. Transient failures (`is_retryable_error`)
never touch the counter.

Cost-cap gated: before each filing it checks today's spend against
`ANTHROPIC_DAILY_COST_CAP_USD` and stops cleanly once reached, so a large
backlog heals across days rather than blowing the budget. Re-runnable and
idempotent; continue-on-failure so one bad filing does not abort the rest.

Needs the Anthropic credential and DB path; no EDGAR user agent (no fetch).
`--dry-run` needs only the DB path. Output is JSON-line structured events to
stdout; exits non-zero if `--dry-run` finds healable orphans or if any heal fails.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

from filings_orchestrator.classify.retry import is_retryable_error
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
    increment_classify_attempt,
    list_orphaned_accessions,
    load_filing_document,
)

_DEFAULT_DAILY_COST_CAP_USD = 5.00

# A filing whose deterministic classification failures reach this count is
# abandoned (dead-lettered) and skipped by normal runs. Three attempts across
# reconciler runs is enough to clear transient noise the in-call backoff missed
# while bounding wasted spend on a genuine poison record.
_MAX_CLASSIFY_ATTEMPTS = 3


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
    parser.add_argument(
        "--force",
        action="store_true",
        help="Also retry abandoned (dead-lettered) filings past the attempt limit.",
    )
    args = parser.parse_args()

    db_path = get_config_str("FILINGS_DB_PATH", default="/var/lib/filings-watcher/filings.db")
    engine = open_engine(db_path)

    attempt_limit = None if args.force else _MAX_CLASSIFY_ATTEMPTS
    orphans = list_orphaned_accessions(engine, max_attempts=attempt_limit)
    # Abandoned = orphans excluded from the work set by the attempt limit.
    total_orphans = len(orphans) if args.force else len(list_orphaned_accessions(engine))
    already_abandoned = total_orphans - len(orphans)
    emit(
        "reclassify_orphans_started",
        orphans=len(orphans),
        already_abandoned=already_abandoned,
        dry_run=args.dry_run,
        force=args.force,
    )

    if args.dry_run:
        for accession in orphans:
            emit("orphan_found", accession=accession)
        emit(
            "reclassify_orphans_completed",
            reclassified=0,
            failed=0,
            newly_abandoned=0,
            skipped=0,
            stopped_at_cap=False,
            remaining=len(orphans),
            already_abandoned=already_abandoned,
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
    newly_abandoned = 0
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
            retryable = is_retryable_error(exc)
            # Only deterministic (non-transient) failures count toward abandonment:
            # a transient outage must not park otherwise-healthy filings.
            if not retryable:
                attempts = increment_classify_attempt(engine, accession)
                if attempts >= _MAX_CLASSIFY_ATTEMPTS:
                    newly_abandoned += 1
                    emit(
                        "classification_abandoned",
                        accession=accession,
                        attempts=attempts,
                        error_class=type(exc).__name__,
                        message=str(exc),
                    )
            emit(
                "reclassify_failed",
                accession=accession,
                error_class=type(exc).__name__,
                message=str(exc),
                retryable=retryable,
            )
            continue

        reclassified += 1
        emit("reclassify_completed", accession=accession, reduce_failed=reduce_failed)

    emit(
        "reclassify_orphans_completed",
        reclassified=reclassified,
        failed=failed,
        newly_abandoned=newly_abandoned,
        skipped=skipped,
        stopped_at_cap=stopped_at_cap,
        remaining=len(orphans) - reclassified,
        already_abandoned=already_abandoned,
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
