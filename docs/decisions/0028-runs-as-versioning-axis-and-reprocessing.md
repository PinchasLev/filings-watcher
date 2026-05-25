# 0028. Runs as the versioning axis, and the corpus-reprocessing model

- **Status:** Proposed
- **Date:** 2026-05-26

## Context

ADR 0011 made classifications append-only and version-tagged, keyed on
`(accession_number, item_number, classifier_version)`, with a window-function
read that selects the latest row per key. ADR 0027 adds a reduce stage that
collates per-Item classifications into filing-level events. Designing the
reprocessing and "current view" semantics for that two-layer model surfaced two
problems the existing approach does not handle.

**The output set of a run can change size between runs.** A reduce run decides
how many events a filing has. An earlier run may under-merge — three events for
the Diversified Energy filing (anchored on Items 1.01, 5.02, 7.01) — while a
later run merges correctly into two (anchored on 1.01 and 5.02; the 7.01
furnishing folds into the appointment). A read that takes the latest row *per
logical identity* keeps the latest row for each anchor independently. The 7.01
event has no successor in the later run, so its earlier-run row remains the
maximum for that identity and surfaces anyway — a dangling event the newer,
better run explicitly decided should not exist. Per-identity selection
resurrects orphans whenever a newer run emits a smaller set than an older one,
and with reduce, a changing set size is the normal case. The same latent flaw
exists for classifications if the set of substantive Items changes between
classifier versions; it has simply never been exercised, because re-
classification has never been run.

**Identical configuration does not guarantee identical output.** Classification
is a function of the LLM, which is non-deterministic and can drift under a fixed
prompt and model name. Two runs sharing a `classifier_version` may legitimately
disagree. ADR 0011's unique index treats a same-version re-run as a no-op
(`INSERT OR IGNORE`), which silently discards that differing output and the
record of the drift. Version-hash identity is therefore the wrong identity.

ADR 0011 also deferred the re-classification CLI "to the first concrete need."
That need has arrived — both the reduce backfill over the existing corpus and
ongoing re-classification as the classifier evolves — so the reprocessing model
is settled here rather than improvised per feature.

## Decision

**A run is a first-class entity and the versioning axis. The current view of a
filing is the complete output of its latest run, selected as a unit.**

- **`runs` ledger.** A new table holds one row per processing pass: `run_id`
  (monotonic primary key, and therefore the ordering for "latest"), `stage`
  (`classify` or `reduce`), the configuration snapshot (model, prompt/reducer
  hash, `taxonomy_version`), lifecycle (`started_at`, `finished_at`, `status` —
  running, succeeded, failed, partial), and scope (counts, trigger). Run-level
  facts live here once rather than denormalized across output rows, and the
  ledger is itself the record of the reprocessing capability — including runs
  that failed, are in progress, or produced no rows.

- **`run_id` is the identity; version strings are metadata.** Every output row
  (events from the outset; classifications produced from this point forward)
  carries `run_id`. Each deliberate re-run is a new run with a new `run_id`,
  independent of whether any code changed — because the LLM is a source of
  variation we do not control. `classifier_version` / `reducer_version` describe
  a run on its `runs` row; they no longer identify output rows.

- **Within-run retry-idempotence; across-run preservation.** Uniqueness within a
  run is `(run_id, accession_number, COALESCE(item_number,''))` for
  classifications and `(run_id, accession_number, anchor_item_number)` for
  events. A resumed or retried run reuses its `run_id`, so re-execution skips
  already-written rows (`INSERT OR IGNORE`). A new deliberate re-run is preserved
  in full, never collapsed into a prior run.

- **Current view = latest run per filing, wholesale.** For a filing at a stage,
  the current output is every row of the run with the greatest `run_id` that
  produced output for that filing — not a per-key maximum. Selecting a run's
  output as a unit means an anchor the latest run did not emit simply does not
  appear; orphan resurrection cannot occur.

- **Reprocessing is explicit and per-stage, with no re-fetch.** Bringing the
  corpus (or a subset) up to date is an operator-invoked run of a single stage
  over stored data. Filing text is immutable, so re-classification and re-reduce
  re-run a downstream stage on stored content; they do not re-fetch from EDGAR.
  (Re-fetch and re-parse are a separate, rarer pass, needed only when the
  extraction logic changes.)

- **No historical retrofit.** Existing classifications are left untouched; no
  synthetic `run_id` is backfilled onto them. A filing is brought current by
  re-running it, which writes new run-stamped rows that supersede the old per
  the latest-run rule — not by rewriting history. Append-only storage and the
  audit trail (ADR 0011) already preserve the past; reprocessing adds to it.

- **Provenance.** A reduce run records which classify run supplied its inputs, so
  an event is reproducible to the exact classifications it collated (ADR 0011's
  derived-artifact reproducibility contract). A consequence is that events may
  lag classifications between reduce runs; the lag is explicit and is closed by
  re-running reduce.

## What this amends

- **ADR 0011.** The unique index gains `run_id`; the read changes from "latest
  row per item" to "latest run per filing"; "same-version re-run is idempotent"
  becomes "within-run retry is idempotent, across-run re-runs are preserved."
  The core of 0011 — append-only, version-tagged, never updated in place — is
  unchanged and in fact reinforced.
- **ADR 0027.** Its "Idempotence" paragraph (the `(accession,
  source_classifier_version, reducer_version)` triple) is replaced by the
  within-run/across-run rule above. The anchored event key
  `(accession, anchor_item_number)` stands as the *within-run* logical identity;
  selection of the current event set across runs is run-based, not a per-anchor
  maximum.

## Alternatives considered

### Latest row per logical identity (per-key selection)

Rejected. It resurrects orphaned outputs whenever a newer run emits a smaller
set than an older one — the dangling-event case above. Correct current-view
semantics require selecting a run's output as a unit.

### Version-hash identity (de-duplicating same-version re-runs)

Rejected. Identical configuration does not guarantee identical LLM output, so
collapsing same-version re-runs silently discards legitimately different results
and the record of model drift. It also forecloses comparing two runs of the same
configuration, which is a useful drift and stability study.

### A `run_id` column without a `runs` table

Rejected as insufficient — though the column is retained regardless, as the
per-row stamp. Run-level metadata (configuration, lifecycle, scope) would
otherwise be denormalized across every output row, with no representation for a
run that failed, is in progress, or produced no rows, and no orderable run
identity. The table holds run-level facts once and serves as the reprocessing
ledger.

### Retrofit historical rows under synthetic runs

Rejected. It rewrites history to no benefit; append-only storage and the audit
trail already protect the past, and re-running a filing is the supported path to
a current state. Designing for forward change is preferable to backfilling the
past.

### Auto-refetch from EDGAR during reprocessing

Rejected. Filing text is immutable, so reprocessing re-runs a downstream stage on
stored content. Re-fetch and re-parse are a distinct pass warranted only by a
change to the extraction logic, not by a classifier or reducer change.

## Consequences

- **Easier:** the current view is correct when output-set sizes change between
  runs; the orphan-resurrection class of bug cannot occur.
- **Easier:** the `runs` ledger is both the reprocessing capability's control
  surface and an operational and audit record of every pass, including failures.
- **Easier:** ADR 0011's reproducibility contract is realized — an event traces
  to the classify run and rows it collated.
- **Easier:** comparing two runs of the same configuration (LLM-drift and
  stability studies) is possible, since same-version re-runs are preserved.
- **Harder:** reading "current" is a two-step pattern — resolve the latest run
  per filing, then return its rows — rather than a flat select. This is the same
  family as the window-function reads ADR 0011 already uses.
- **Accepted:** row count grows with each re-run (append-only); the trade-off
  ADR 0011 already accepts, now also at the events layer.
- **Accepted:** events may lag classifications between reduce runs; the lag is
  explicit and closed by re-running reduce.
- **Accepted:** legacy classifications carry no `run_id`; a uniform run-based
  read over the classifications table is unnecessary now, because the public read
  path uses events and the pinned `event_classifications` join. When a
  re-classification run lands, its rows carry `run_id` and supersede the legacy
  rows for that filing under the latest-run rule.

## Deferred

- **The reprocessing CLI(s)** and operator experience — selecting filings,
  dry-run, progress reporting — land with the implementing PR.
- **Bringing the classifications read path under run-based selection** — deferred
  until a re-classification run actually exercises it; the public read path uses
  events and pinned joins until then.
- **Corpus-scale run orchestration** — parallelism and resumption beyond
  `INSERT OR IGNORE` — deferred until corpus volume requires it.
- **Cross-filing event identity (entity resolution)** — unchanged from ADR 0027;
  owned by the later cross-filing clustering layer.
