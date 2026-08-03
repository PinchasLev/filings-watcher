"""Attach hand labels to the raw risk->events dump, producing the realization gold set.

Label rule (the "explicit-disclosure" bar): gold_realized=true ONLY when a subsequent event
EXPLICITLY discloses the concrete thing the risk factor's CORE subject warns about — the
hypothetical has plainly occurred or is concretely advancing, stated in the event's own
disclosure. Anything requiring inference, a peripheral facet, a merely-related topic, a generic
earnings release, routine/favorable financing, or a big salient event loosely matched → false.

gold_events lists the event indices that would count as a correct realization (a prediction is a
true positive only if it names one of them). Reruns re-score for free against these labels.
"""

import json

pairs = json.load(open("/tmp/pairs.json"))

# index -> (gold_realized, [acceptable event indices], rationale)
POS = {
    10: (
        [3],
        "Align: CLO (EVP, Chief Legal & Regulatory Officer) resigned to join Illumina — "
        "an explicit loss of a named executive officer, realizing the key-personnel / "
        "executive-retention risk the factor is about.",
    ),
    41: (
        [2],
        "Aflac: 8-K discloses an actual cyber intrusion (unauthorized third-party access to "
        "Aflac Japan systems) — the IT-security / data-confidentiality risk has explicitly "
        "materialized.",
    ),
}
# Negatives worth an explicit rationale — the traps the judge keeps falling into.
TRAP = {
    5: "Hormel labor-relations/availability/cost risk; the only leadership event is a CEO "
    "appointment (succession) — a peripheral facet, not the core labor risk. The canonical "
    "over-match to reject.",
    13: "MarketAxess broad operations/client-dependency umbrella; the ICE merger is a "
    "change-of-control, not a realization of operational dependency. A big salient event must "
    "not match a loosely-related umbrella risk.",
    16: "MarketAxess retain-senior-management risk; amending severance/change-of-control terms "
    "ahead of the merger is a retention measure, not an executive loss.",
    24: "Realty Income REIT distribution-flexibility risk; routine borrowings do not explicitly "
    "evidence a distribution-driven constraint (requires inference).",
    25: "Realty Income debt/preferred-financing risk; issuing or refinancing notes increases "
    "exposure but is not the adverse CONSEQUENCE the risk warns of (a downgrade, covenant "
    "breach, default, or forced sale would be). Rotation vs. real leverage deterioration is a "
    "magnitude question — quantitative/B — out of scope for this prose-only feature.",
    27: "Diamondback substantial-indebtedness risk; a credit-facility maturity extension is "
    "favorable refinancing, not an adverse or exposure-increasing realization.",
    29: "Diamondback 'may be unable to obtain financing' risk; obtaining an amended/expanded "
    "facility is the opposite of the risk materializing.",
    32: "Diamondback retain-personnel/key-person risk; Stice's planned transition to "
    "non-executive Chairman (he stays on the board) is orderly succession, not a failure to "
    "retain.",
    47: "AMC film-distributor-access risk; the equity offerings are unrelated capital-raising. "
    "Capital-raising is not a catch-all realization.",
}
DEFAULT = "No subsequent event explicitly discloses the specific thing this risk factor names."

out = []
for i, r in enumerate(pairs):
    if i in POS:
        realized, evs, why = True, POS[i][0], POS[i][1]
    else:
        realized, evs, why = False, [], TRAP.get(i, DEFAULT)
    out.append(
        {
            "id": i,
            "company": r["company"],
            "acc": r["acc"],
            "seq": r["seq"],
            "section": r["section"],
            "bucket": r["bucket"],
            "risk_text": r["text"],
            "chg": r["chg"],
            "events": r["events"],
            "gold_realized": realized,
            "gold_events": evs,
            "rationale": why,
        }
    )

path = "orchestrator/eval/realization_gold.jsonl"
with open("/home/pinchaslev/projects/filings-watcher/" + path, "w") as f:
    for rec in out:
        f.write(json.dumps(rec) + "\n")

pos = sum(1 for x in out if x["gold_realized"])
print(f"wrote {len(out)} records to {path}: {pos} positive, {len(out) - pos} negative")
print("positives:", [x["id"] for x in out if x["gold_realized"]])
