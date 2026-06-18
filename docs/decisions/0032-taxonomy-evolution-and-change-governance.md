# 0032. Taxonomy evolution and change governance

- **Status:** Proposed
- **Date:** 2026-06-18

## Context

The classification taxonomy is not fixed and never will be. The first
`exhibit-ab` evaluation (PR #111, run 2026-06-18) made this concrete: of 33
exhibit-bearing filings, 21 carried `other_material`, and two of the four
exhibit-driven re-classifications were forced into `ma_activity` only because the
taxonomy has no home for them — an asset sale (Denali's $195M Priority Review
Voucher) and a project financing (CIM's $600M facility). The catch-all is large
and specific events are being shoehorned into the nearest leaf. We will add
categories, and over time we will also split, merge, and re-parent them.

The pieces to handle this already exist but are governed informally. The taxonomy
is **already two-tier**: a leaf `EventType` (tier-2) plus a coarse `EventDomain`
(tier-1) reached by a deterministic `EVENT_TO_DOMAIN` rollup — a post-hoc mapping,
not a hierarchical classifier, chosen in ADR 0010. Classifications carry a
`taxonomy_version` and the reclassification/versioning machinery is established
(ADR 0011), as is run-based reprocessing for replay (ADR 0028). What is missing is
a **disciplined policy for changing the taxonomy**: which changes are safe, how
versions move, when the historical corpus is replayed, what invariants must always
hold, and how a change is shown to be an improvement before it ships. Without that
discipline, taxonomy changes are ad-hoc edits to `taxonomy.py` whose blast radius
on historical data and downstream consumers is decided case by case.

This ADR governs taxonomy *change*. It does not fix the taxonomy's contents — the
category set is a versioned artifact that this policy governs, so it can evolve
without amending this ADR.

## Decision

### 1. The taxonomy is a versioned, replayable projection — never a source of truth

The authoritative record is the immutable filing text (body + exhibits, the latter
stored in full since ADR 0031 / migration 009). A classification is a *derived
projection* of that text under a specific taxonomy version; any taxonomy, past or
future, is reproducible by re-classifying the stored text — no EDGAR re-fetch
(ADR 0028). Taxonomy changes therefore never lose information: the worst case is
the compute cost of a replay. This is the event-sourcing posture ADR 0011
established, made explicit as the governing principle for change.

The taxonomy *contents* — the `EventType` leaves, their descriptions, the
`EventDomain` tier, and the `EVENT_TO_DOMAIN` rollup — are the versioned artifact.
This ADR governs how they change; it does not enumerate them. §5 persists each
version of that artifact as a durable snapshot so every historical classification
stays fully interpretable under the exact taxonomy it was made against.

### 2. Change classes and version semantics

Every taxonomy change is one of two classes, and the class determines the version
bump and the replay policy:

- **Additive** — a new leaf, a new domain, a new per-domain catch-all, or a
  description refinement, with no change to the meaning of an existing leaf. Safe:
  existing classifications remain valid under their original version; only future
  (and optionally replayed) classifications can use the new option. Bumps the
  taxonomy version's **minor** component (e.g. `v1` → `v1.1`).
- **Breaking** — renaming, splitting, merging, or re-parenting an existing leaf, or
  any change that alters what an existing label means. Existing rows' labels may no
  longer mean what they say. Requires a deliberate migration and bumps the
  **major** component (`v1.x` → `v2`).

`TAXONOMY_VERSION` moves from an opaque `"v1"` to a two-part `major.minor` value so
the class of every historical change is legible from the version alone.

### 3. Replay policy per change class

- **Additive** changes default to **forward-only**: new classifications use the new
  taxonomy; the historical corpus is left under its prior version and reclassified
  lazily or on demand. A full-corpus replay (via a `reduce-corpus`-style sweep) is
  *optional* — run it when the new category materially improves historical views
  and the compute cost is justified.
- **Breaking** changes **require** a decision recorded with the change: either a
  full-corpus replay to the new version, or an explicit, documented acceptance that
  the corpus holds mixed major versions.
- A mixed-version corpus is always valid to read: selection follows the
  latest-run-wins rule already in force for the events layer (ADR 0028), and every
  row remains interpretable under the `taxonomy_version` it carries (ADR 0011).

### 4. Structural invariants (must hold across every change)

These are the properties no taxonomy change may violate. They are the contract the
governance protects.

- **Catch-all escape hatches at both tiers.** The taxonomy must always let the
  classifier say "I don't know" without forcing a wrong specific label or dropping
  the event. This requires *two distinct* kinds of catch-all, which mean different
  things and have different expected frequencies:
  - A **per-domain catch-all leaf** (`<domain>/other`, e.g. a financial event of an
    unrecognized sub-type) — "I am confident about the **domain**, not the leaf."
    This is the common case and the direct fix for the forced-fit problem: CIM's
    project financing belongs in a financial-domain catch-all, not `ma_activity`.
    *This is a new structural element this ADR introduces;* today only a single
    global catch-all exists.
  - A **global catch-all** (the existing `other_material` → `catchall` domain) —
    "I cannot confidently place this in **any** domain." This must stay rare; its
    rate is a health metric, and a rising rate is the signal to add a domain.
  Splitting the one global catch-all into per-domain catch-alls is what lets
  two-tier rescue the "known-domain, unknown-leaf" majority while reserving the
  global catch-all for genuinely novel events.

- **Materiality is an orthogonal magnitude axis, never a type.** Whether an event
  *matters* is independent of *what kind* of event it is, and must never be folded
  into the type taxonomy: the same leaf varies in materiality by magnitude (a $5M
  and a $5B `debt_issuance` are both `debt_issuance`). Materiality is `is_material`
  today — a boolean proxy — and is expected to become a **derived threshold on an
  extracted signal magnitude** once the extraction/aggregation layer exists, at
  which point "non-material" is simply "signal below threshold" and the judgment
  moves from the model to code (consistent with the LLM-classifies / code-computes
  split). The orthogonality survives that evolution. A "routine / administrative"
  *type* bucket — for filings that carry no substantive event — is a legitimate
  taxonomy entry and is distinct from the materiality flag and from the catch-alls.

- **One leaf per section; the domain follows mechanically.** The model assigns a
  single leaf `EventType` per classified section; the `EventDomain` is derived by
  deterministic rollup (ADR 0010), not a separate model decision. A coarse-first
  *model* pass (classify the domain, then the leaf within it) is **not** adopted by
  default; it is reserved for selective use where measurement shows a granular
  accuracy bottleneck (see §7).

### 5. Schema: persist the coarse domain on the row, and each taxonomy version as a snapshot

A classification row stores only the leaf today (`event_type`, e.g.
`ma_activity`); the coarse domain (`operational`) is not stored but computed on
demand by the `domain_for` lookup in application code. That has two costs. First,
the database cannot answer a coarse question — "how many financial-domain events
this month?" — with a plain `GROUP BY`, because the domain is not a column, so the
mapping would have to be re-applied in code or hard-coded into each query. Second,
because the leaf→domain map is itself part of the taxonomy and can change,
recomputing an old row's domain under a newer map would silently re-label history.

The decision: write the rolled-up domain into an `event_category` column on the
row at classification time, alongside the leaf. This is a deliberate
denormalization — the domain is derivable from the leaf, but storing it makes
coarse aggregation a pure `GROUP BY` (which the future aggregation layer needs) and
records the domain *as it was decided* under that row's `taxonomy_version`, so later
edits to the map never retroactively change what a historical row meant. Additive
migration; existing rows are backfilled by applying `domain_for` under their
recorded version.

Storing the leaf and its domain records *what was decided* — but not *what the
classifier was allowed to choose from*, the set of leaves and descriptions it saw.
Today that choice-set exists only in `taxonomy.py` at the commit a given
`taxonomy_version` was current; reconstructing it for a historical row is git
archaeology and assumes the version label was bumped faithfully on every edit. The
`classifier_version` prompt hash *fingerprints* the choice-set — enough to detect a
difference or confirm a match — but cannot reproduce it. That is below the audit
and reproducibility bar of ADR 0011 ("what did the system say about filing X on
date D, and under what options").

So each taxonomy version is also persisted as a durable, queryable snapshot. This
ADR introduces three new auxiliary tables that mirror the two tiers:

- **`taxonomy_versions`** — one row per cut version (PK `taxonomy_version`), the
  anchor that marks a version as created and carries its metadata (the `major` /
  `minor` split, the cut timestamp).
- **`taxonomy_domains`** — the tier-1 contract, PK `(taxonomy_version, domain)`:
  each domain and its description (and the per-domain catch-alls), FK to
  `taxonomy_versions`.
- **`taxonomy_leaves`** — the tier-2 menu, PK `(taxonomy_version, leaf)`: each leaf,
  its description, and the domain it rolls up to (FK to `taxonomy_domains`), FK to
  `taxonomy_versions`.

A historical classification carries `taxonomy_version`, the leaf, and the
`event_category` domain, so the exact menu it faced is a join away — reproducibly,
without git archaeology or trusting manual version bumps. **Classifications
reference these snapshots by natural key** — the `(taxonomy_version, event_type)`
value already on the row — **never a surrogate id.** A classification is an
immutable as-of record (the reference-data vs. transaction-data distinction), so it
stores the tag as a *value* and joins to the snapshot for descriptions, rather than
depending on a mutable id that a rename could silently re-point. Each version stores
its full leaf+domain set (not a delta), so every version is self-contained; the
volume is trivial (versions × leaves). The in-code taxonomy stays the authoring
surface; the snapshot is written from it, idempotently, when a version is cut (at
migrate time), so editing the taxonomy is unchanged and the database gains the as-of
record. This is the concrete form of the "versioned artifact" in §1. It captures the
choice-set; the surrounding non-taxonomy prompt framing remains fingerprinted by
`classifier_version`, and can itself be snapshotted later if full verbatim-prompt
reproduction is ever required.

**Immutability and atomic creation.** A version's snapshot is frozen once cut.
Cutting a version writes its `taxonomy_versions` anchor plus all of its domain and
leaf rows in a *single transaction*, so creation is atomic — a version exists in
full or not at all. `UPDATE` and `DELETE` on the three tables are blocked, so a
label once recorded under a version can never be changed or deleted (the audit
guarantee). SQLite has no role-based `REVOKE`, so this is enforced with
`BEFORE UPDATE`/`BEFORE DELETE` triggers that abort; on Postgres it would be a
`REVOKE`. Appending a leaf to an already-cut version is prevented by the single,
write-once-per-version populate path; a trigger rejecting inserts against an
existing anchor is an optional belt-and-suspenders, adopted only if that discipline
proves insufficient. The `classifications` table is already append-only by design
(ADR 0011 — re-classification appends a new row, latest wins); the same
`UPDATE`/`DELETE` block should be applied to it as well, after a write-path audit
confirms nothing legitimately mutates a classification.

### 6. Expansion is data-driven, from the catch-all

New leaves are discovered, not guessed: periodically mine the global catch-all and
the low-confidence / forced-fit classifications, cluster what lands there, and
introduce leaves for the recurring patterns. The first `exhibit-ab` run already
nominates `asset_sale/divestiture` and `debt/project_financing`; issue #16
(workforce reduction / restructuring under Item 2.05, parked since 2026-05-14
pending recurrence evidence) is a pre-existing candidate of exactly this shape and
becomes the inaugural case run through this process. This keeps the taxonomy shaped
by what filings actually disclose rather than by anticipation. The bar for adopting
a candidate is the §7 A/B gate rather than a fixed occurrence count — a measured
improvement is a stronger test than a threshold.

### 7. Every taxonomy change is evaluated before adoption

A taxonomy change is adopted only after an offline A/B evaluation against the prior
taxonomy on a sample (the `exhibit-ab` harness generalized — the change is the
treatment arm). The evaluation reports leaf migrations, catch-all reduction, and
confidence deltas; a human reviews a handful of changed cases (the harness measures
"changed," not "correct"). This makes the A/B *capability* ADR 0011 anticipated a
*required gate*. A coarse-first second model pass (§4) is itself a change subject to
this gate, adopted only where it measurably fixes a leaf-accuracy bottleneck within
a domain, and then only for the domains that need it — never paid for globally on
faith.

## Alternatives considered

### Flat taxonomy with ad-hoc edits and a single catch-all

The status quo. Rejected: it has no policy for the blast radius of a change, no
distinction between safe and breaking edits, and a single global catch-all that
conflates "unknown sub-type" with "unknown domain" — exactly the bucket the
`exhibit-ab` run showed overflowing.

### A full hierarchical (coarse-first) classifier as the default

Classify the domain with one model call, then the leaf within it with a second.
Rejected as the default for the reasons ADR 0010 already gave, plus doubled
per-section cost and latency. The deterministic rollup gets the coarse contract for
free; a second model pass is held in reserve for selective, measured use (§7).

### Fold materiality into the type taxonomy (a "non-material" type)

Rejected: it conflates two independent axes and discards magnitude information —
"non-material `debt_issuance`" becomes inexpressible. Materiality stays an
orthogonal axis on a path from boolean to derived signal threshold.

### Govern change purely by reclassifying the whole corpus on every edit

Always replay to a single current version, so the corpus is never mixed. Rejected
as the blanket rule: it makes every additive tweak pay a full-corpus compute cost
and removes the audit value of versioned history (ADR 0011). Full replay stays
available and is the default for breaking changes, but is not forced on additive
ones.

## Consequences

- Taxonomy growth becomes routine and safe: additive changes ship forward-only with
  a minor version bump and no obligation to touch history, while breaking changes
  carry an explicit, recorded migration decision.
- The per-domain catch-alls shrink the forced-fit problem the `exhibit-ab` run
  exposed, and reserve the global catch-all as a clean "add a domain" signal.
- A stable coarse tier plus a persisted `event_category` gives the coming
  aggregation layer a durable contract to roll up against, insulated from tier-2
  churn.
- Persisting each taxonomy version as a snapshot makes historical classifications
  fully reproducible — any past row can be joined to the exact choice-set it faced
  — at the cost of a small write per version cut and three new tables to maintain.
- Enforcing append-only immutability on the snapshot (and classifications) commits
  us to trigger-based protection on SQLite (no role-based `REVOKE`), which is
  backend-specific and must be re-expressed if we ever move to Postgres. We accept
  that for the guarantee that a recorded label is never silently changed or deleted.
- We commit to A/B-gating taxonomy changes (real evaluation cost and a human review
  step) and to maintaining the per-domain catch-alls and the rollup map as
  first-class taxonomy elements.
- We accept a corpus that can hold mixed taxonomy versions; readers must continue to
  honor each row's `taxonomy_version`, and the version string carries more meaning
  (`major.minor`) than before.

## What this amends

- **ADR 0010** — keeps the per-Item granularity and the post-hoc domain rollup;
  evolves the domain layer by adding per-domain catch-all leaves and persisting the
  derived domain.
- **ADR 0011** — keeps the versioning/history/reclassification model; adds the
  change-class taxonomy, the `major.minor` version semantics, the A/B gate, and the
  persisted per-version taxonomy snapshot that makes its audit/reproducibility goals
  fully attainable.
- **ADR 0028** — reuses run-based reprocessing as the replay mechanism for taxonomy
  migrations; adds the per-change replay policy.

## Deferred

- The concrete next taxonomy revision (the specific new leaves and any domain-set
  changes) is a change *processed through* this governance, not part of this ADR.
- The extraction/aggregation layer that turns `is_material` into a derived signal
  threshold is a later component; this ADR only fixes the orthogonality invariant
  it must respect.
