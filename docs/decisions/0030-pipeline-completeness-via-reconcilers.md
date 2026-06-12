# 0030. Pipeline completeness via derived-layer reconcilers; dedup is discovery-identity only

- **Status:** Proposed
- **Date:** 2026-06-12

## Context

The per-filing pipeline runs five steps: resolve the filing reference, fetch
its body, persist the filing (`upsert_filing_document`), classify each Item
(the map stage, [ADR 0027](0027-two-pass-classification-filing-level-events.md)),
and reduce the classifications into events (the reduce stage). Both ingest
paths ([ADR 0021](0021-realtime-8k-ingest-via-daily-index.md),
[ADR 0029](0029-near-realtime-8k-ingest-via-atom-feed.md)) dedup candidates with
`select_seen_accessions`, which keys solely on the `filings` primary key.

This overloads one fact — *a row exists in `filings`* — to answer two unrelated
questions: **identity** ("is this the same filing we already discovered?") and
**completion** ("has it finished every pipeline stage?"). The `filings` row is
written at step 3 of 5, before classify runs. So a filing is marked "seen, do
not touch" before it is processed. A failure at step 4 (an Anthropic error
mid-classify, a validation rejection, a DB error) leaves a **seen-but-not-done**
filing, and because dedup reads "seen" as "done," every later tick skips it
permanently. The result is an orphan: a `filings` row with zero
`classifications` rows, never re-classified by any path. The same conflation
blocks *deliberate* reprocessing of the live pipeline: dedup is correctly
idempotent on the immutability of EDGAR filings ("no change in the fetched
content"), but because that check is the only gate, there is no supported
"re-run this filing through classify" verb. The reduce stage escaped this only
because [ADR 0028](0028-runs-as-versioning-axis-and-reprocessing.md) built
`reduce-corpus` as a separate reprocess path; classify never got one.

These are three faces of one flaw — orphans, no deliberate re-run, no
resume-from-stage — and ADR 0028 already anticipated the fix in its Deferred
section ("bringing the classifications read path under run-based selection";
"corpus-scale run orchestration ... resumption"). This ADR settles the
completeness-and-resumption model so it is uniform across stages rather than
improvised per bug.

## Decision

**Completeness is a property of each derived layer, defined as "current-version
output exists for this filing" — never as the existence of a `filings` row.
Ingest dedup is discovery-identity only. Each derived layer owns a reconciler
that fills its gaps; the live tick is the eager path, not the arbiter of
done-ness.**

- **Layered completeness.** `filings` is the immutable source (complete when the
  row exists). `classifications` is the derived map layer, complete for a filing
  when a classification exists for the current `classifier_version`. `events` is
  the derived reduce layer, complete when events exist for the current
  `reducer_version`. The absence of current-version derived output *is* the
  checkpoint: it records, without a separate status field, exactly which stage a
  filing still needs.

- **Dedup answers identity, not completion.** `select_seen_accessions` continues
  to prevent re-*ingesting* the same accession as new work. It must no longer be
  the signal that a filing is fully processed. Completion and resumption are
  owned by the per-layer reconcilers below.

- **A reconciler per derived layer.** `reduce-corpus` already reconciles the
  events layer. This ADR adds the symmetric reconciler for the classify layer:
  select filings lacking current-version classifications and re-run the map
  stage over stored content. An orphan is simply the degenerate case — a filing
  missing *any* classification — so the orphan backfill is one instance of the
  general reconciler, not a special-purpose tool.

- **Resume-from-stage falls out.** Because each stage consumes the prior stage's
  *persisted* output, reconcilers compose without a cursor: a filing that
  classified but failed to reduce is picked up by the reduce reconciler and runs
  *only* reduce; the classify reconciler leaves it alone because its
  current-version classification already exists. Deliberate re-runs fall out the
  same way — bump a version stamp and the corresponding reconciler re-derives.

- **Bounded attempts and dead-letter for poison records.** Pure re-derivation is
  safe to repeat only when a stage is cheap or free. Transient failures
  (rate-limit, 5xx, network) and credit/auth failures are effectively free — no
  completion, no tokens billed — and self-heal, so reconcilers may retry them
  freely. A *deterministic output-validation* failure (the model completes,
  tokens are billed, and the output is rejected) would re-burn tokens forever. So
  each derived stage tracks per-filing failed attempts, counts **only** these
  token-burning deterministic failures, and after a bounded number marks the
  filing **abandoned** at that stage and emits an alarm-eligible event. Abandoned
  filings are excluded from the reconciler's work set (no storm) and surfaced for
  the operator (no silent loss). "How many times have we failed" is not a
  function of the source data, so this is the irreducibly stateful part of the
  model — the one thing the declarative layer cannot express.

- **No re-fetch on reprocessing.** Consistent with ADR 0028: filing text is
  immutable, so classify reconciliation re-runs the map stage on stored body
  text; it does not re-fetch from EDGAR. Re-fetch remains a distinct, rarer pass
  warranted only by a change to the extraction logic.

## Alternatives considered

### A single status column keyed per accession

Rejected. The pipeline crosses a map→reduce **grain change**: map fans out (one
filing → N Item classifications), reduce fans in to a new key (N classifications
→ M anchored events). A status keyed on the accession cannot checkpoint a stage
whose unit of work is not the accession. Stage outputs must each be addressable
at their own grain — which the `events(run_id, accession, anchor)` +
`event_classifications` tables already are.

### A pure per-row state machine (checkpoint-and-resume only)

Rejected as the *primary* model. Tracking explicit per-item pipeline position is
the traditional approach, but at this grain it duplicates information the
derived-output tables already carry (absence-of-output is the checkpoint), and it
does not survive the grain change above. We adopt its one irreplaceable element —
bounded-attempt state and a dead-letter — layered over the derived outputs (the
"A-over-B hybrid"), rather than as the whole design.

### Pure re-derivation (reconcilers only, no attempt state)

Rejected as insufficient. It cannot bound a deterministic poison-pill record,
whose unbounded re-derivation would burn tokens to the daily cap and stall ingest
until UTC midnight. The bounded-attempt dead-letter closes this.

### Adopt an orchestrator (Dagster / Airflow / Temporal) now

Rejected as premature — see the next section. At single-node, single-SQLite,
low-thousands-of-filings scale, an orchestrator's metadata store, scheduler, and
worker runtime cost more operational surface than they save. We adopt the
*patterns* (Dagster's software-defined-asset / reconciler model; bounded retries
and dead-letter) hand-rolled on SQLite and systemd timers, and defer the *tool*
until a trigger below fires.

## When to revisit (orchestration thresholds)

This decision is sound only while the pipeline stays small and single-node. Adopt
a real orchestrator when any of these observable signals appears, and record
which:

1. **Exactly-once side effects.** A stage begins emitting an external effect that
   must fire once (alerts, email, webhooks, downstream writes). Hand-rolled
   re-derivation cannot guarantee this; reach for **Temporal**-style durable
   execution or a transactional outbox. The alarms work is the likely first
   trigger.
2. **DAG shape outgrows a linear "fill missing per layer."** Branching or
   conditional stages, sub-pipelines (exhibit ingestion), or cross-entity fan-in
   (the Stage-3 cross-filing reduce) where dependencies stop being a straight
   line. Reach for **Dagster** — its asset graph is this ADR's reconciler model,
   productized.
3. **One node cannot keep up.** When throughput needs distributed workers,
   backpressure, and work distribution (the corpus-reprocessing speedup already
   on the roadmap). Reach for a distributed queue/scheduler, or a big-data engine
   if the grain warrants it.
4. **Backfill/replay ergonomics become a burden.** When partial re-runs, progress
   observation, and lineage are ad-hoc SQL plus CLIs and the operator wants a UI.
   **Dagster/Airflow** UIs earn their keep.
5. **Team scale.** More than one or two people authoring stages and needing shared
   guardrails and conventions.

Until then, the systemd-timer eager tick plus per-layer reconciler CLIs *are* the
orchestrator, at the right size for the workload.

## What this amends

- **ADR 0028.** Picks up its Deferred items: the classify reconciler and the
  resumption model. The run/`run_id`/latest-run-wins machinery is unchanged and
  is the substrate this builds on; classify outputs come under run-based
  selection as their reconciler lands.
- **ADR 0021 / 0029.** `select_seen_accessions` is re-scoped from "is this filing
  done?" to "have we discovered and stored this filing's raw content?" The live
  tick remains the low-latency first pass; it no longer defines completeness.

## Consequences

- **Easier:** orphans cannot persist — a filing missing its current-version
  classification is, by definition, reconciler work, not a dead row.
- **Easier:** deliberate reprocessing of the live pipeline is supported and
  uniform — bump a version, run the layer's reconciler — symmetric with
  `reduce-corpus`.
- **Easier:** resume-from-point-of-failure needs no cursor or per-row status;
  composing reconcilers over persisted stage outputs gives per-stage resume for
  free.
- **Easier:** a deterministic poison record is bounded and surfaced, not silently
  dropped or infinitely retried.
- **Harder:** a small schema addition for per-stage attempt/abandonment state, and
  the discipline that every new derived layer ships with a reconciler and a
  version stamp from the outset.
- **Accepted:** completeness is now a query (anti-join against current-version
  output), not a flat boolean — the same family as ADR 0028's latest-run reads.
- **Accepted:** the model is hand-rolled and will need migration to a real
  orchestrator once a threshold above fires; the cost of that migration is the
  price of not paying for an orchestrator now.

## Deferred

- **The reconciler CLI(s) and the dedup re-scoping** — selecting filings, dry-run,
  progress reporting, and the exact mechanism by which the live tick stops gating
  completeness — land with the implementing PRs, starting with the classify
  reconciler (which heals the existing orphan backlog).
- **The attempt/abandonment schema and bounded-retry policy** (max attempts, which
  error classes count) — settled with the prevention slice; the orphan-healing
  slice does not require it.
- **Bringing the classifications read path fully under run-based selection** —
  remains as ADR 0028 left it, exercised when a re-classification run lands.
