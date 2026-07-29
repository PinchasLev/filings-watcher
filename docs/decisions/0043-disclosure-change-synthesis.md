# 0043. Disclosure-change synthesis: from a change list to a read

- **Status:** Proposed
- **Date:** 2026-07-26

## Context

ADR 0042 built the prose-change pipeline (A) end to end — segment → embed →
diff → judge — and it now runs live on timers. A demo backfill of four names
validated the *detection*: Plug Power surfaced 40 material risk-factor changes
(impairments, restructuring, hydrogen-strategy risk), Chegg 13 (going-concern,
workforce cuts), Boeing 10 (a 101-day strike, the Spirit acquisition close), and
Coca-Cola just 2 — the quiet control proving the pipeline does not manufacture
noise.

But the *surface* undersells the work. The company page renders the changes as a
**flat, undifferentiated list of N tagged blocks**: a "New"/"Revised" tag, a
category tag, the highlighted passage, and a one-line summary — repeated forty
times. Three gaps make that unreadable:

1. **No prioritization.** A going-concern addition and a reworded cybersecurity
   paragraph sit side by side, equally weighted. We have confidence scores and do
   not use them to rank or tier.
2. **No direction.** "New" / "Revised" describe the *mechanical diff operation*,
   not the *meaning*. A reader does not care that a block was revised; they care
   that leverage risk got **worse**.
3. **No synthesis.** There is no top-line answer, so the reader must reassemble
   the story from forty fragments — doing the comprehension work we promised to do
   for them. That is why forty changes reads as noise and two reads as fine: the
   reader, not the product, is doing the sense-making.

We shipped a **diff viewer**; the product thesis (ADR 0042) is **comprehension**.
The changes are the *evidence*; the page is missing the *answer*. This decision
adds the layer that turns the list into a read.

## Decision

Present each filing's disclosure changes as an **inverted pyramid** — answer in
five seconds, story in thirty, evidence on demand — backed by three additions to
the pipeline and surface:

### 1. A headline: two judged axes + honest counts

At the top, a **two-axis** risk-shift verdict — a **direction** (worsening / easing /
mixed) and an **intensity** (major / moderate / minor) — plus the raw counts (worse /
eased / material, with the two periods compared). Composed for display, that reads
"Major worsening" or "Minor worsening"; the intensity axis is what carries the
Plug-Power-vs-Coca-Cola contrast in the *label*, not only in the counts.

Both axes are **judged by the synthesis reduce** (§2) as a holistic, severity-aware
read of all the changes at once; the **counts are computed by code** and shown beside
them as an independent factual cross-check. Neither axis is a **numeric distress
score** — a "0.73" invites "how did you compute that?", a question we cannot defend
without a validated model, and it drags us back toward the black-box *signal* framing
we abandoned for comprehension. Two coarse categorical axes are the ceiling; we do not
add a third or a scale.

> **Amended 2026-07-26.** This ADR originally had *code* roll up the headline from
> the per-change directions. That was wrong: materiality is captured as a *boolean*,
> so a count is severity-blind — a new going-concern and a reworded boilerplate risk
> both count as "one material worse." No count threshold can tell that a single
> severe change is not "stable," nor that seven marginal worsenings are. The reduce
> is the *only* actor that reads every finding with its meaning at once, so it is the
> only one that can weigh severity. Judging the net direction is **comprehension, not
> arithmetic** — reading forty findings and characterizing the whole — which is the
> model's strength, not a forbidden calculation. The bounded-operator boundary is
> preserved exactly: **code still owns every number** (the counts), and those counts
> sit beside the label so the two signals keep each other honest (a "Stable" label
> next to "38 worse / 0 eased" is a visible contradiction and a natural place for a
> later sanity flag). `Mixed` is principled here — the model picks it when *meaningful*
> worsening and *meaningful* easing coexist, not when counts happen to balance.
>
> The two sides of the ledger are handled symmetrically in the pipeline, but the
> *eased* side is inherently noisier: Item 1A is a downside-only disclosure by SEC
> rule (Reg S-K Item 105), so a removed or softened risk is ambiguous (genuine
> resolution vs. reorganization/boilerplate cleanup) in a way a newly-added risk is
> not. The reduce is instructed to be skeptical that easing reflects real
> improvement. Positive business outlook does not live in 1A at all — it lives in
> MD&A and the numbers, which is a later arc.
>
> This headline is therefore scoped to the **risk-factor section**: "worsening"
> means *disclosed risks worsened*, not *the company is doing badly*. The synthesis
> row is keyed by section, so when MD&A and the numbers (B) arrive they are separate
> per-section reads; composing them into a single whole-company headline — including
> the high-value case where risk factors worsen while the outlook stays rosy (the
> A×B divergence signal) — is a deliberate later step, not built toward now.
>
> **Amended 2026-07-27 — split into two axes.** The first cut of the reduce-judged
> headline was a *single* categorical direction (deteriorating / improving / mixed /
> stable). We ran it on the four demo names and every one came back "deteriorating,"
> Coca-Cola (three modest but genuinely material worsenings) included. The failure was
> structural, not a bad prompt: one label was carrying two independent facts — *which
> way* the risk moved and *how much* the overall picture moved — and collapsed to the
> more salient one (direction), so the magnitude contrast vanished from the label and
> survived only in the counts. The fix is to model the two facts as two axes:
> **direction** (worsening / easing / mixed) and **intensity** (major / moderate /
> minor). This is not added complexity — the original four labels *decompose* into the
> grid (deteriorating = worsening × major/moderate; stable = minor; improving = easing
> × major/moderate; mixed = mixed), which is the proof the single label was conflating
> them. Both remain coarse LLM judgments (no score); the eased-side skepticism and the
> section-scoping above are unchanged. We hold the line at two axes — a third
> ("business outlook") is a *different section's* signal that composes in later, not a
> field on the risk-factor verdict.

### 2. A synthesis paragraph: a Stage-3 reduce

A short generated **thesis paragraph** and a **top-effects** shortlist — *"this
year's filing signals accelerating financial distress: new impairment risk
($785M), a second restructuring plan, and hydrogen-strategy execution risk."* This
is a **reduce over the material change verdicts**, composing with the exact
map-reduce pattern already used for classification (map = per-change judge, which
already emits a distilled one-sentence summary; reduce = compose those summaries).

Crucially the reduce consumes the **distilled** per-change summaries, never the
raw block prose, so it stays a single bounded LLM call whether there are forty
changes or, once later arcs add breadth, many more. Its output is **stored and
versioned** (`synthesis_version` = model + prompt hash, re-derived gap-driven like
every other stage) so the company page and any later cross-company feed read the
same synthesis, and a prompt change re-derives auditably.

### 3. Direction and a governed category on each change

Two fields move onto the materiality verdict:

- **Direction** (`worse` / `eased` / `neutral`) — an **LLM judgment**, not a
  mechanical derivation: added is usually worse, dropped is usually eased but
  sometimes a reorganization, changed is either. The LLM already reasons direction
  in its explanation prose (the ADR 0042 e2e run showed this); we make it a
  first-class field. Direction — the *meaning* — leads the display; the mechanical
  added/changed/dropped is demoted to secondary detail.
- **Governed category** — the judge picks the change's theme from a **bounded,
  governed vocabulary** rather than emitting free text. The demo produced
  `restructuring`, `new business risk - hydrogen strategy`, and
  `strategic alternatives/M&A` as sibling categories; grouping on free text
  fragments forty changes into thirty-five singleton themes — no better than the
  flat list. A bounded vocabulary is what makes the grouping cohere. It reuses the
  **governance pattern** of ADR 0032 (a fixed enum with a catch-all, versioned by
  content hash) but is a **distinct vocabulary** — risk themes, not 8-K event
  types — so it does not touch the event `TAXONOMY_VERSION`.

### Governance organizes the top; it must not flatten the bottom

The tight governance stops at the **theme boundary**. Top-level themes are drawn
from the bounded vocabulary so the page groups cleanly. The **drilldown beneath a
theme preserves the full, specific, ungoverned detail** the LLM produced per change
— the "$785M impairment", the "workforce reduction 22% → 56%", the exact passage
and its explanation. That specificity is *where the insight lives*; a reader opens
a theme precisely to get past the label to the particulars. So we govern the
labels for organization and leave the evidence rich for insight — never normalize
the explanations into fixed fields, which would trade the feature's value for tidy
uniformity.

### Organize by theme, not by section or pipeline

The drilldown groups changes by **theme** (Liquidity & solvency, Restructuring,
Litigation, …), not by the section or pipeline that produced them. A reader thinks
in themes; the pipeline is our concern, not theirs. This is a **reader-first choice
justified today**, on the risk-factor changes we have now — not a bet on future
inputs (see below).

### Concrete for A; deliberately not built ahead for B

This arc builds **only against A's risk-factor change verdicts**. We do **not**
construct a source-agnostic "finding" abstraction, a numbers half (B) producer, an
MD&A producer, an A×B divergence step, or a cross-pipeline taxonomy. B has not
landed; we cannot yet see what its data actually looks like, and designing a
unifying schema against imagined data is the over-complexity trap. The synthesis
reduce and the theme grouping are built for the changes in front of us. When a
second input source is real, we will see its shape and generalize *then* — generalize
on second use, not first. If theme grouping later helps fuse A and B, that is a
bonus we collect when B exists, not a design we carry its weight for now.

### Bounded-operator boundaries

The LLM judges each change's category (from the governed enum), direction, and
one-line explanation; the reduce then judges the filing's headline direction and
writes the thesis and top-effects from those distilled judgments. **Code** computes
the counts, groups by theme, and stores/versions the synthesis. The division is
clean: **numbers are code's job, judgment is the model's** — the LLM never computes a
count or a score, and code never writes prose or characterizes a direction. Every LLM
output is validated (enum membership, schema) before code trusts it.

## Alternatives considered

### A numeric distress score

Rejected. Spurious precision we cannot defend without a validated model, and it
reintroduces the black-box *signal* framing we left behind for comprehension. A
categorical direction plus honest counts gives the same at-a-glance signal
honestly.

### Keep the flat list, add client-side sort/filter

Rejected. Sorting forty equally-shaped rows still leaves the reader to synthesize
the story. The gap is a missing *answer*, not a missing *sort order*.

### Normalize the per-change explanations into fixed structured fields

Rejected — it flattens exactly the detail that carries the insight. Governance
belongs at the theme label, not the evidence. The drilldown stays rich prose plus
citation.

### Build the general "finding" abstraction now so B slots in

Rejected. B's data shape is unknown; a source-agnostic schema built against
imagined data is speculative complexity. Build concrete against A; generalize when
B is real.

### Group by section (Risk Factors / MD&A / Numbers) rather than by theme

Rejected for now. A reader thinks in themes, and today there is a single section
anyway. Revisit the top-level cut only when multiple sections exist and a
section-first view earns its place.

### Compute the synthesis live on each page render

Rejected. Store and version it: reproducible, reusable by a later cross-company
feed, cheaper (one reduce per filing, not per view), and auditable when the prompt
changes — consistent with every other stage being a persisted, versioned artifact.

## Consequences

**Easier / what we gain:** a company page that *reads* — headline, thesis, and
theme-grouped evidence — instead of a forty-item dump; a scannable per-filing
verdict that is the prerequisite for a future cross-company feed (a feed of
headline verdicts is pushable; a feed of forty-item dumps is not); a stored,
versioned synthesis reused across surfaces; and a direction field that makes
"whose risk language deteriorated this period" a query.

**Harder / costlier — new burdens:** one more recurring LLM call per filing (the
reduce — cheap, cost-capped, but new); a **governed category vocabulary** to
define and maintain, with the same evolution-governance surface as the event
taxonomy (ADR 0032); and a **new trust surface** — the synthesis paragraph is
model-written prose shown directly to the user, where prior surfaces showed
per-item judgments. We mitigate: the paragraph composes only from
already-validated per-change findings (it introduces no new claims), it is
versioned and re-derivable, and the evidence sits one click beneath it for
verification.

**Committed to:** direction and category as first-class, validated fields; a
reduce-judged two-axis headline (direction + intensity) beside code-computed counts,
never an LLM score; synthesis as a bounded reduce over distilled findings; governed
themes with rich, ungoverned drilldown detail; and concrete-for-A, generalizing only
when B is real.

**Accepted losses / deferrals:** the cross-company discovery feed and its naming;
B, MD&A, and the A×B divergence as synthesis inputs; and any trend/baseline view —
all later arcs, none built toward here.

## PR sequence

Built one at a time, off fresh `main`, in order.

1. **Direction + governed category on the verdict** *(LLM contract + persistence)*
   — add a `direction` field and a bounded `category` enum (with a catch-all,
   content-hash versioned) to the materiality judge's forced tool schema and
   prompt; a migration adds the columns; re-judging supersedes prior verdicts via
   `judge_version`. Exercise the migration under `go test -race ./...` as well as
   pytest (migrations have two appliers).
2. **Synthesis reduce + storage** *(LLM reduce + persistence)* — a `synthesize`
   step that, per filing, reduces the distilled material verdicts into a judged
   headline (direction + intensity), a thesis paragraph, and a top-effects list, while
   code computes the counts shown beside the headline; a migration for a synthesis
   table keyed by (accession, section, model, judge_version, synthesis_version); a
   cost-capped, resumable, gap-driven reconciler CLI. (The intensity axis was added in
   a follow-up once the single-axis headline was measured to under-discriminate — see
   the §1 amendment.)
3. **Surface reorganized (Go read side)** — replace the flat list with the
   inverted pyramid: headline direction + counts, the synthesis paragraph + top
   effects, then a theme-grouped, collapsible drilldown that preserves each
   change's full explanation, direction, citation, and needs-review flag.

**Explicitly deferred to later arcs:** the cross-company feed and section naming ·
B / MD&A / A×B as synthesis inputs · the source-agnostic finding abstraction ·
trend and baseline views.

## Amendment: stable filings get a standing-risk summary

The synthesis above only fires when a filing has ≥1 material change. A filing diffed
against its prior year with **zero** material changes therefore produced no synthesis
and fell off the radar entirely — but "no change" is not "no risk." A stable filer
still carries real, material risk; it simply did not move year-over-year. Surfacing
that ("Caterpillar's risk factors were materially unchanged") is itself a signal, and
absence is not a non-result.

So a stable filing gets a **standing-risk summary**: a second reduce that reads the
**current** Risk Factors section (there is no diff to reduce) and summarizes the
company's principal *standing* risks — what they ARE, not what changed. It is stored in
the same `filing_change_synthesis` table with `headline_direction = 'stable'` and
`headline_intensity = 'none'` (all counts zero), so no migration is needed.

Design boundaries held:

- **Trigger precisely.** "Stable" means *diffed, fully judged, zero material changes* —
  never "could not diff." A single-year filing (no prior) or an unparseable one (no
  diff row) is *not* stable, just uncovered; calling it stable would be a lie. The gap
  query keys on `filing_diffs`, excludes any filing with an un-judged change (so we
  never pre-empt the judge), and is disjoint from the change-synthesis gap query.
- **Own version.** `standing_synthesis_version` is separate from `synthesis_version`
  (model + a hash of its own, distinct prompt), so the two reduce paths re-derive
  independently.
- **The badge stays honest.** `intensity = 'none'` is code-set (never the model): the
  magnitude axis measures *change*, and a stable filing's change magnitude is zero. The
  standing-risk *severity* lives in the prose (thesis + top standing risks), not the
  badge — so the same badge never means two different things.
- **Surface split.** Stable cards render on the **company page** (the "state of this
  company" view: "Unchanged — and here are the standing risks"), but are **excluded
  from the cross-company movement feed and its counts** — that feed answers "what
  *moved*," and stable filings would swamp it. A future "stable" filter chip could
  admit them to the feed on demand.

The stable pass shares the reconciler's per-run budget (change synthesis runs first;
the stable pass takes the remainder), so a tick's LLM calls stay bounded by `--limit`
and both drain across runs.
