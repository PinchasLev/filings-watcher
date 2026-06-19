"""Shared offline-eval core for classifier A/B harnesses (ADR 0031/0032).

Generic over two arms of classifying the same filings — a **baseline** (control)
and a **candidate** (treatment): exhibit-ab runs exhibits-off vs exhibits-on,
classify-ab runs taxonomy version A vs B. The diff and aggregate are computed in
code, never asked of the model (the bounded-operator principle). This is the
reusable core promoted out of exhibit-ab on its second use.

Each filing flattens to comparable *units* — one per Item, plus a whole-filing
fallback — and the two arms are diffed unit by unit. Deltas are oriented
candidate-minus-baseline; `other_material_reduction` is positive when the
candidate moves units out of the global catch-all.
"""

from __future__ import annotations

from filings_orchestrator.classify import FilingClassification

_WHOLE = "__whole_filing__"

_OTHER_MATERIAL = "other_material"


def units(classification: FilingClassification) -> dict[str, tuple[str, bool, float]]:
    """Flatten a FilingClassification to {unit_key: (event_type, is_material, confidence)}.

    A unit is one Item (keyed by item_number) or the whole-filing fallback — the
    comparable shape the two arms are diffed on.
    """
    out: dict[str, tuple[str, bool, float]] = {}
    for item in classification.items:
        c = item.classification
        out[item.item_number] = (c.event_type.value, c.is_material, c.confidence)
    if classification.whole_filing is not None:
        c = classification.whole_filing
        out[_WHOLE] = (c.event_type.value, c.is_material, c.confidence)
    return out


def diff_filing(
    accession: str,
    baseline: FilingClassification,
    candidate: FilingClassification,
) -> dict[str, object]:
    """Diff the baseline and candidate classifications of one filing, unit by unit.

    Each unit reports both arms' event_type, materiality, and confidence, whether
    each flipped, and the confidence delta (candidate minus baseline). A unit
    present in only one arm (rare — item splitting is deterministic on the same
    body) is reported as such.
    """
    a = units(baseline)
    b = units(candidate)
    unit_rows: list[dict[str, object]] = []
    for key in sorted(set(a) | set(b)):
        base = a.get(key)
        cand = b.get(key)
        if base is None or cand is None:
            unit_rows.append({"unit": key, "present_in": "candidate" if cand else "baseline"})
            continue
        unit_rows.append(
            {
                "unit": key,
                "baseline_event_type": base[0],
                "candidate_event_type": cand[0],
                "event_type_changed": base[0] != cand[0],
                "baseline_is_material": base[1],
                "candidate_is_material": cand[1],
                "is_material_changed": base[1] != cand[1],
                "baseline_confidence": round(base[2], 4),
                "candidate_confidence": round(cand[2], 4),
                "confidence_delta": round(cand[2] - base[2], 4),
            }
        )
    return {"accession": accession, "units": unit_rows}


def summarize(results: list[dict[str, object]]) -> dict[str, object]:
    """Aggregate per-filing diffs into the headline A/B numbers.

    Counts comparable units, how many flipped event_type or materiality, the mean
    confidence delta, and the net change in `other_material` (the catch-all the
    candidate aims to shrink). All computed in code.
    """
    comparable = 0
    event_changed = 0
    material_changed = 0
    delta_sum = 0.0
    other_material_baseline = 0
    other_material_candidate = 0
    for r in results:
        unit_rows: list[dict[str, object]] = r["units"]  # type: ignore[assignment]
        for u in unit_rows:
            if "confidence_delta" not in u:
                continue
            comparable += 1
            if u["event_type_changed"]:
                event_changed += 1
            if u["is_material_changed"]:
                material_changed += 1
            delta_sum += float(u["confidence_delta"])  # type: ignore[arg-type]
            if u["baseline_event_type"] == _OTHER_MATERIAL:
                other_material_baseline += 1
            if u["candidate_event_type"] == _OTHER_MATERIAL:
                other_material_candidate += 1
    return {
        "filings": len(results),
        "comparable_units": comparable,
        "event_type_changed": event_changed,
        "event_type_changed_pct": round(100 * event_changed / comparable, 2) if comparable else 0,
        "is_material_changed": material_changed,
        "mean_confidence_delta": round(delta_sum / comparable, 4) if comparable else 0,
        "other_material_baseline": other_material_baseline,
        "other_material_candidate": other_material_candidate,
        "other_material_reduction": other_material_baseline - other_material_candidate,
    }
