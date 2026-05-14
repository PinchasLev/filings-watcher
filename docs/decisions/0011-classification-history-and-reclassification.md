# 0011. Classification history, versioning, and re-classification policy

- **Status:** Accepted
- **Date:** 2026-05-14

## Context

Classifier output is a function of four inputs that change at different rates:

| Input | Mutability | When it changes |
|---|---|---|
| Filing text | Immutable | Never (filings are immutable post-filing) |
| Prompt | Per code change | Every PR that touches `classifier.py` |
| Model | Per upgrade | Anthropic releases; our choice of model |
| Taxonomy | Per code change | Every PR that touches `taxonomy.py` (e.g., the `exec_compensation` addition in PR #15) |

A classification produced at time T1 with classifier configuration C1 may differ from one at T2 with classifier configuration C2 — even for the same filing. Any persistence layer that ignores this presents inaccurate views in dashboards, breaks reproducibility of derived analytics, and loses audit trail.

The schema must support three concrete capabilities now or in the near future:

- **Audit-style queries** — *"what did the system say about filing X on date D?"*
- **A/B classifier comparison** — *"which classifier configuration is more accurate on the eval set?"*
- **Reproducibility of derived artifacts** — *"recompute the March 2026 stress score using the classifications as they existed on March 31, 2026"* (Tier 2)

## Decision

**Classifications are immutable, append-only, and version-tagged.**

Each classification row carries:

- `classifier_version` — model name + short SHA-256 prefix of the system prompt (e.g., `claude-haiku-4-5-20251001+prompt-a1b2c3d4`)
- `taxonomy_version` — a manually-bumped constant (`v1`, `v2`, ...) bumped on every change to the `EventType` enum, its descriptions, or the `EventType → EventDomain` mapping
- `classified_at` — UTC timestamp when the row was written

A UNIQUE INDEX over `(accession_number, COALESCE(item_number, ''), classifier_version)` enforces:

- Same-version re-runs are idempotent (no-op via `INSERT OR IGNORE`)
- New versions produce new rows alongside existing ones
- Whole-filing classifications (where `item_number IS NULL`) participate in the constraint correctly via the `COALESCE` to empty string

Classifications are never updated in place. When a classifier changes, **historical rows preserve what the system said at the time.** Re-classifying older filings under a new version is an explicit operation, not implicit.

## Why this is not formal bitemporal modeling

The schema captures two natural time axes — `filing_date` (when the event happened in the world) and `classified_at` (when the system recorded its judgment). This is sometimes called "lite bitemporal" or "level-1 bitemporal" — the data layer is structurally aware of both axes.

What this does NOT commit to:

- **Editable history.** Formal bitemporal modeling (with `valid_from`/`valid_to` and `recorded_from`/`recorded_to` interval columns) lets you retroactively rewrite the system's past assertions. This schema does not — assertions are append-only.
- **Bitemporal join semantics.** Queries like *"what did we believe on date A about the state of the world on date B"* are expressible against this schema with timestamp filters but do not use formal bitemporal-join operators.
- **Independent valid-time and transaction-time intervals.** Each classification is a point-in-time assertion, not an interval.

The simpler model is sufficient for the classification problem because we **never edit past assertions** — when our judgment changes, we add a new assertion, not rewrite the old one. The append-only semantics preserve the system's actual record of when it asserted what; historical assertions cannot be rewritten after the fact.

Formal bitemporal modeling is the right answer when downstream consumers depend on retroactive corrections (banking ledgers, regulatory filings, employment records). That is not our situation.

## A/B testing as a side benefit

The same `classifier_version` column that handles taxonomy evolution naturally supports A/B testing of classifier configurations:

- Run Haiku and Sonnet on the same filings → two rows per filing, distinguished by `classifier_version`
- Disagreement query: JOIN on accession + item, compare `event_type` across versions
- Scored against eval-set ground truth, this is the standard classifier-comparison workflow

We do not build the A/B *workflow* (parallel-run orchestration, comparison dashboards) in v0 — only the schema substrate. The eval set and comparison views land in later PRs.

## Reproducibility contract for derived artifacts

This ADR commits to a forward-looking rule for Tier 2 derived artifacts (stress scores, anomaly signals, training sets, eval-set predictions):

**Every derived artifact must record its input snapshot context** — which `classifier_version` produced its inputs, the IDs of contributing classification rows, and the time window covered. This is the practical "third time dimension": valid-time and transaction-time live on the source data; decision-time lives on the derived artifact.

Without this rule, a derived signal becomes irreproducible the moment any contributing classification is re-run under a new version. With it, signals are replayable and auditable.

The rule is **policy now, code later** — enforced when the first derived artifact lands.

## Re-classification policy

Re-classifying historical filings under a new classifier configuration is an **explicit, deliberate operation**, never implicit. The mechanism:

1. Operator runs a CLI command (deferred — to be added with the first re-classification need)
2. The command iterates over historical filings and runs the current classifier
3. Each new classification is written as a new row with the new `classifier_version`
4. Old rows remain unchanged

The decision *whether* to re-classify is per-operator, per-event. Adding `exec_compensation` (PR #15) created a known gap: historical filings with Item 5.02(e) content sit as `other_material`. We do not auto-backfill; if and when we want the historical view aligned, it becomes an explicit run.

## Alternatives considered

### Update-in-place classifications

Rejected. Minimal storage cost but loses every historical view, breaks audit, and makes derived-artifact reproducibility impossible.

### Soft-delete (mark rows as superseded)

Rejected. More complex than append-only without adding capabilities the simpler model lacks. Append-only with a `latest` query is functionally equivalent and structurally cleaner.

### Formal bitemporal modeling

Rejected. Substantial complexity overhead (four timestamp columns, interval-arithmetic in queries, specialized tooling) without a use case that justifies it for classification data. Reconsidered if the project later ingests data sources that need retroactive corrections.

### Separate history table (audit log alongside a "current" table)

Rejected. Doubles the storage, requires synchronized updates across two tables, and complicates queries. The single append-only table with a `latest` query pattern is cleaner.

## Consequences

- **Easier:** Audit, A/B, and reproducibility questions are all answerable from a single schema. Tier 2 work doesn't require schema migration.
- **Easier:** The classifier can be improved freely — add categories, change prompts, swap models — without worrying about losing history.
- **Easier:** Migration to Postgres (per ADR 0008) is a connection-string change; the SQL is portable.
- **Harder:** Historical row count grows monotonically. At v0 traffic this is negligible (thousands of rows per year per watchlist); at scale we might add partitioning by `classified_at`. Deferred until volume requires it.
- **Harder:** "Latest classification for this filing" is a query, not a column. SQL idioms exist (`ORDER BY classified_at DESC LIMIT 1` per item via window functions). Acceptable.
- **Accepted commitment:** Derived-artifact reproducibility rule must be honored when Tier 2 work begins. Stated now to avoid retrofitting.

## Deferred

- **CLI for explicit re-classification of historical filings.** Lands when there is a first concrete need to backfill (e.g., to align historical `other_material` with a newer taxonomy that has `exec_compensation`).
- **Distribution-monitoring query.** ADR 0010 commits to monitoring `other_material` share and per-category distribution; the query becomes a CLI command or a periodic job once the corpus is large enough to inspect.
- **Deploy storage layer.** The SQLite file lives on local instance storage in deploy (App Runner ephemeral, Fargate ephemeral, or EC2 EBS); durability comes from `litestream` continuously streaming the SQLite WAL to S3, or from migration to Postgres. The full deploy-storage ADR is deferred until first deploy.
- **Postgres migration.** The connection-string swap is mechanical; the trigger conditions are listed in ADR 0008.
