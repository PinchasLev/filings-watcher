# 0027. Two-pass classification: filing-level events over per-item classifications

- **Status:** Proposed
- **Date:** 2026-05-25

## Context

The classifier (ADR 0009/0011) processes each 8-K Item in isolation:
`classify_filing` loops over the filing's substantive Items, making one Claude
tool-use call per Item with only that Item's text in the prompt. The module
docstring states the intent plainly — "items are classified independently."
That isolation produces two failure modes, both observed in production
(Diversified Energy Co, CIK `0001922446`):

1. **Redundant detection across sibling Items.** A substantive Item and its
   companion disclosure are classified as separate events with no awareness of
   each other. The Diversified Energy 8-K of 2026-05-21 discloses one appointment
   under both Item 5.02 (the board action) and Item 7.01 (the Reg-FD
   press-release furnishing); each is independently classified `exec_appointment`,
   surfacing as two separate entries for a single real-world event. The
   5.02 + 7.01-furnishing pairing is among the most common 8-K structures, so the
   redundancy is systematic rather than incidental.

2. **Reference-resolution failure.** An Item that incorporates a sibling by
   reference cannot see it. The same filing's Item 2.03 reads "the substance is
   incorporated by reference from Item 1.01"; lacking 1.01's content, it fell
   back to low-confidence `other_material` — even though Item 1.01 (the
   $850 million ABS notes issuance) sits in the same filing. This is a loss of
   correctness, not merely of presentation.

The per-Item *reasoning* is sound; the *orchestration* is the limitation. An
analyst reads an 8-K as a whole, recognizes that several Items describe one
event, and resolves "see Item X" cross-references. The system needs a stage that
does the same — without discarding the per-Item signal, which ADR 0011 makes
auditable and version-tagged.

ADR 0011 anticipated this. Its reproducibility contract commits that "every
derived artifact must record its input snapshot context — which
`classifier_version` produced its inputs, the IDs of contributing classification
rows, and the time window covered," enforced "when the first derived artifact
lands." A collated, filing-level event is exactly that first derived artifact.

## Decision

Add a second classification pass — a **reduce** stage — that collates the
per-Item classifications of one filing into deduplicated, filing-level
**events**, and persist events as a new layer linked back to the contributing
classification rows.

**Pipeline.** The LangGraph gains a node: `START → classify → reduce → END`. The
`classify` node is unchanged (the **map** stage: one tool-use call per
substantive Item, the raw signal). The `reduce` node makes one additional
tool-use call whose input is the *compact* per-Item results of this filing —
`(item_number, event_type, is_material, confidence, reasoning)` — and emits a
list of filing-level events, each carrying a consolidated `event_type`,
materiality, confidence, a summary, and the set of Item numbers it subsumes.
Reduce is a pass-through for single-classification filings (the whole-filing
fallback, or a filing with a single substantive Item).

**Persistence.** Two additions (migration 004), both append-only and
version-tagged in the spirit of ADR 0011:

- **`events`** — one row per filing-level event: `accession_number`,
  `anchor_item_number` (see *Event identity* below), `event_type`,
  `event_domain`, `is_material`, `confidence`, `summary`, `reducer_version`
  (reduce model + reduce-prompt SHA prefix, mirroring `classifier_version`),
  `source_classifier_version` (the `classifier_version` of the Item
  classifications it consumed — the ADR 0011 input-snapshot context),
  `taxonomy_version`, and `reduced_at`.
- **`event_classifications`** — a join table `(event_id, classification_id)`
  recording the exact contributing classification rows. This is the literal
  realization of ADR 0011's "IDs of contributing classification rows" rule.

Events are written *after* their source classifications so the join references
concrete row IDs. An event's `event_type` is the reduce stage's judgment and may
differ from every contributing Item's label (the Diversified Energy filing's 1.01
`ma_activity` and 2.03 `other_material` collapse into one financing event); the
raw classifications retain their original labels, so re-labeling at the event
layer never rewrites history.

**Event identity.** An event has no source-given identifier the way an Item
(`item_number`) or a filing (`accession_number`) does; it is emergent from the
reduce stage. The autoincrement `id` serves only as a surrogate key for
foreign-key references and is not the event's logical identity. The logical
identity is `(accession_number, anchor_item_number)`, where the anchor is the
primary substantive Item the event centers on — Item 5.02 for the appointment,
Item 1.01 for the financing event. Companion disclosures (Reg-FD furnishings,
exhibits, incorporations by reference) attach to the anchored event through the
join table but do not define it, so peripheral membership changes between runs
do not change the identity. The anchor, rather than the full Item set, is the
key precisely because the set may churn while the substantive core does not.

This identity is stable as long as the anchoring is stable. A different
`reducer_version` that genuinely regroups a filing's Items may select a different
anchor, and therefore a different identity — the correct semantics, since the
system's judgment about the event boundaries has itself changed. Identity that
survives re-classification, and cross-filing identity (one real-world event
disclosed across several filings), are entity-resolution problems owned by the
deferred cross-filing clustering layer and out of scope here.

**Idempotence.** The unit is the *run*, not the row: a reduce over
`(accession_number, source_classifier_version, reducer_version)` either exists or
does not; re-running the same triple is a no-op that reproduces the same anchored
events. This generalizes ADR 0011's per-row uniqueness to per-run, because reduce
decides how many events a filing has; the anchored logical key above identifies
events within a run in the absence of a source-given per-row key. Reads take the
events of the latest `reducer_version` per filing (the window-function `latest`
pattern the service already uses).

**Framing.** The pattern is map-reduce with two prompts — a deterministic
two-node DAG — rather than an autonomous multi-agent system. The reduce stage
makes a single call with a single responsibility, which keeps the trace legible.
The initial implementation consumes the existing per-Item output (`event_type`
and `reasoning`), which already names entities and surfaces "incorporated by
reference" cross-references; no change to the pass-1 (map) schema is required to
begin.

## Alternatives considered

### Presentation-layer de-duplication only

Group a filing's Items visually and collapse same-`event_type` entries in the
template. Rejected as cosmetic: it cannot address reference resolution (Item 2.03
remains mislabeled in the underlying data), and the event is still recorded twice
beneath the presentation.

### Full sibling context in every map call

Pass the whole filing into each Item's prompt. Rejected: O(n²) context per
filing, cost that grows with Item count, and one row still emitted per Item with
no collation — the redundancy (mode 1) remains.

### Single whole-filing classification (no map)

One call classifies the entire concatenated filing. Rejected: it loses the
per-Item audit trail ADR 0011 depends on, places the full text (roughly 15,000
tokens or more on a multi-Item filing) into a single prompt that does not scale
on outliers, and conflates raw signal with collation. Map-reduce keeps the map
stage parallelizable and bounds reduce to compact summaries (on the order of
hundreds of tokens) regardless of filing size.

### Autonomous multi-agent system

Independent agents with their own goals and tools coordinating. Rejected as
miscalibrated: the problem is a single deterministic collation, not autonomous
negotiation. Map-reduce with two prompts achieves the same result with
substantially less complexity, cost, and trace opacity — the simplest mechanism
that solves the problem, rather than the most capable one available.

### Heuristic (non-LLM) de-duplication by `event_type` within a filing

Merge same-type Item classifications mechanically. Rejected as insufficient: it
handles the straightforward same-type case but not reference resolution (mode 2),
nor merges across differing Item labels (1.01 and 2.03 in the example above), and
it is brittle to taxonomy nuance.

## Consequences

- **Easier:** both failure modes are fixed at the layer where the context
  exists. Reduce sees the 5.02 and 7.01 summaries and emits one appointment
  event; it sees the 2.03 and 1.01 summaries and resolves the obligation to the
  ABS notes.
- **Easier:** events provide the first-class event entity that both the editorial
  agent and the cross-filing early-warning objective require. The change also
  establishes that foundation, and it discharges ADR 0011's deferred
  derived-artifact reproducibility rule with a concrete first consumer.
- **Easier:** the raw per-Item layer is untouched — auditability, A/B comparison,
  and re-classification (ADR 0011) all continue to hold; events layer cleanly on
  top.
- **Harder:** "latest events for a filing" is a query, not a column (the same
  trade-off ADR 0011 already accepts for classifications).
- **Harder:** evaluation now spans two stages. The Diversified Energy filing
  (CIK `0001922446`) becomes a canonical regression case: it must reduce to
  exactly two events, with the 2.03 → 1.01 reference resolved. The evaluation set
  gains a reduce dimension.
- **Cost:** one extra Claude call per filing, on compact input, with the system
  prompt cached (ADR 0022). Negligible for a nightly/batch cadence; map stays
  parallelizable.
- **Accepted commitment:** events re-label freely at the collation layer; the
  raw classifications are the immutable record of what each Item said in
  isolation. Over-merging by reduce is always recoverable from the raw layer.
- **Accepted commitment:** the service read path migrates from classifications
  to events as the primary list view (home, company, detail), with detail
  expanding to the contributing raw classifications via the join. That is
  downstream PR work, not part of this schema decision.

## Deferred

- **Enriching the pass-1 output** with explicit structured `summary`,
  `entities`, and `cross_references` fields to improve reduce reliability. The
  initial implementation operates on the existing `reasoning` text; evaluation
  determines whether the structured fields justify the schema change (and the
  attendant `classifier_version` bump).
- **Retrieval escalation.** If a summary proves too lossy for reduce to resolve a
  reference (requiring the actual figures in Item 1.01 rather than a summary),
  reduce could fetch the referenced Item's full text — genuine tool-use. This
  escalation should be justified by the reference-resolution failure mode in
  practice rather than built preemptively.
- **Durable and cross-filing event identity.** The anchored key is stable within
  a filing for a given grouping, but not across reduce regroupings or across
  filings. Recognizing that one real-world event spans multiple filings — an
  appointment announced, then later formalized — is entity resolution, owned by
  the deferred cross-filing clustering layer.
- **Service/UI migration** to render events (with raw-classification expansion),
  tracked as the implementing follow-up PR. This ADR flips to **Accepted** when
  the schema + reduce node land.
