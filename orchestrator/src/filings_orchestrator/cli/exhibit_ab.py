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

The diff/aggregate core (`_diff_filing`, `_summarize`) is kept generic so a later
prompt/model A/B reuses it; only the with/without-exhibit toggle is specific here.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from functools import partial

from filings_orchestrator.classify import FilingClassification, classify_filing
from filings_orchestrator.classify.retry import with_retries
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

# Sentinel key for the whole-filing classification (filings with no extractable
# Item sections), so it diffs alongside per-Item units.
_WHOLE = "__whole_filing__"


def _units(classification: FilingClassification) -> dict[str, tuple[str, bool, float]]:
    """Flatten a FilingClassification to {unit_key: (event_type, is_material, confidence)}.

    A unit is one Item (keyed by item_number) or the whole-filing fallback. This
    is the comparable shape the with/without runs are diffed on.
    """
    out: dict[str, tuple[str, bool, float]] = {}
    for item in classification.items:
        c = item.classification
        out[item.item_number] = (c.event_type.value, c.is_material, c.confidence)
    if classification.whole_filing is not None:
        c = classification.whole_filing
        out[_WHOLE] = (c.event_type.value, c.is_material, c.confidence)
    return out


def _diff_filing(
    accession: str,
    with_ex: FilingClassification,
    without_ex: FilingClassification,
) -> dict[str, object]:
    """Diff the two classifications of one filing, unit by unit.

    Returns a structured per-filing record: each unit's event_type and
    confidence under both arms, whether the event_type flipped, and the
    confidence delta (with minus without). Units present in only one arm (rare —
    item splitting is deterministic on the same body) are reported as such.
    """
    a = _units(with_ex)  # with exhibits
    b = _units(without_ex)  # without exhibits
    units: list[dict[str, object]] = []
    for key in sorted(set(a) | set(b)):
        wa = a.get(key)
        wo = b.get(key)
        if wa is None or wo is None:
            units.append({"unit": key, "present_in": "with" if wa else "without"})
            continue
        units.append(
            {
                "unit": key,
                "with_event_type": wa[0],
                "without_event_type": wo[0],
                "event_type_changed": wa[0] != wo[0],
                "with_is_material": wa[1],
                "without_is_material": wo[1],
                "is_material_changed": wa[1] != wo[1],
                "with_confidence": round(wa[2], 4),
                "without_confidence": round(wo[2], 4),
                "confidence_delta": round(wa[2] - wo[2], 4),
            }
        )
    return {"accession": accession, "units": units}


def _summarize(results: list[dict[str, object]]) -> dict[str, object]:
    """Aggregate per-filing diffs into the headline A/B numbers.

    Counts comparable units, how many flipped event_type, how many changed
    materiality, the mean confidence delta, and the net change in `other_material`
    assignments (the share this feature is meant to reduce). Computed in code —
    never asked of the model (see the bounded-operator principle).
    """
    comparable = 0
    event_changed = 0
    material_changed = 0
    delta_sum = 0.0
    other_material_with = 0
    other_material_without = 0
    for r in results:
        for u in r["units"]:  # type: ignore[attr-defined]
            if "confidence_delta" not in u:
                continue
            comparable += 1
            if u["event_type_changed"]:
                event_changed += 1
            if u["is_material_changed"]:
                material_changed += 1
            delta_sum += float(u["confidence_delta"])
            if u["with_event_type"] == "other_material":
                other_material_with += 1
            if u["without_event_type"] == "other_material":
                other_material_without += 1
    return {
        "filings": len(results),
        "comparable_units": comparable,
        "event_type_changed": event_changed,
        "event_type_changed_pct": round(100 * event_changed / comparable, 2) if comparable else 0,
        "is_material_changed": material_changed,
        "mean_confidence_delta": round(delta_sum / comparable, 4) if comparable else 0,
        "other_material_with_exhibits": other_material_with,
        "other_material_without_exhibits": other_material_without,
        "other_material_reduction": other_material_without - other_material_with,
    }


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

        diff = _diff_filing(accession, with_ex, without_ex)
        results.append(diff)
        emit("exhibit_ab_result", **diff)

    emit(
        "exhibit_ab_summary",
        skipped=skipped,
        stopped_at_cap=stopped_at_cap,
        **_summarize(results),
    )


if __name__ == "__main__":
    main()
