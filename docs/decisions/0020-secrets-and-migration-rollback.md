# 0020. Secret seeding and migration discipline for v0

- **Status:** Accepted
- **Date:** 2026-05-18

## Context

The orchestrator calls Anthropic's API and (optionally) LangSmith. Both require API keys. The keys exist on the operator's laptop in a password manager and as shell environment variables during development; production has no place to put them yet. Two related questions arise as the orchestrator moves onto the host:

1. **Where does the API key live in production, and how does it get there?**
2. **The orchestrator owns the database schema and applies migrations on startup. What happens when a migration was wrong?**

Both are operational disciplines that scale differently from single-operator v0 to a team with rotation requirements. Picking the v0 answer without naming the conditions under which it should be revisited risks the lightweight choice hardening into the permanent one past the point where it remains correct.

## Decision

### Secrets

The Anthropic API key (and any equivalent third-party credential — LangSmith, future vendors) lives in **AWS Systems Manager Parameter Store** as a `SecureString`, encrypted with the default account KMS key. The path is `/filings-watcher/<key-name>` (e.g., `/filings-watcher/anthropic-api-key`).

The parameter is **created out-of-band by the operator**, not by Terraform. The operator runs `aws ssm put-parameter` once per key per AWS account. Terraform references the parameter *path* (in IAM policies and SSM document bodies) but does not manage the value.

The host's IAM role gains a narrow inline policy granting `ssm:GetParameter` and `ssm:GetParameters` on `arn:aws:ssm:<region>:<account>:parameter/filings-watcher/*` only. No broader Parameter Store access.

At runtime, the orchestrator (or the SSM document invoking it) fetches the parameter, exports it as `ANTHROPIC_API_KEY` in the process environment, and the existing `load_config()` path picks it up unchanged. The key never lands on disk in plaintext.

### Migrations

Migrations are **forward-only**. The `db/migrations/` directory contains numbered `.sql` files; `apply_migrations()` runs each unapplied file in order and records it in a `schema_migrations` tracking table. There are no paired down files. This matches the default Flyway-community pattern: migrations live with the code, only unapplied files run on each deploy, schema state moves forward monotonically.

Migration files are written to be **additive**, following the expand-and-contract pattern:

- **Expand**: new columns are nullable or have defaults; new tables and indexes are introduced alongside existing ones; the previous schema continues to satisfy the previous code.
- **Migrate**: backfills, dual-writes, or code transitions happen across one or more deploys while both schema shapes are functional.
- **Contract**: destructive changes (drop column, rename, narrow constraint) land in a *separate* migration on a *later* deploy, after the previous generation of code is no longer running.

Rolling back a release SHA is safe when the SHA's migrations were additive: the older binary runs against the newer (additive) schema and ignores fields it does not know about. This is the practical rollback path — redeploy an older SHA via the existing operator-triggered deploy workflow. No down migration runs; the additive schema simply has unused fields.

Snapshot restore (per [ADR 0019](0019-data-persistence-across-instance-replacement.md)) remains the recovery path for the rarer cases:

- A destructive migration ("contract" step) was deployed and is regretted before the next snapshot.
- A data-transformation migration mutated row contents in a way the operator wants to undo.
- A migration was non-additive by mistake, violating the discipline.

Migration files are versioned **with the application code**. Release tarball `abc123` contains migrations `001..NNN`; release tarball `abc122` contains migrations `001..NNN-1`. The deploy SSM document runs `migrate-db` after extracting the tarball and before swapping the symlink, so the running binary always sees a schema it was built against.

## Rationale

### Why Parameter Store and not Secrets Manager

AWS Secrets Manager is the higher-feature option: automated rotation, multi-region replication, cross-account sharing, audit trails. It also costs $0.40 per secret per month plus per-API-call charges, which is a percentage point of the v0 budget envelope per credential.

Parameter Store `SecureString` covers the v0 use case (encrypted at rest, IAM-scoped, retrievable from the instance) at zero ongoing cost. The migration to Secrets Manager is a Terraform change (resource type swap), an IAM scope swap, and an `aws ssm get-parameter` → `aws secretsmanager get-secret-value` swap in the fetch step. The investment is warranted when rotation, audit, or cross-account requirements materialize; not warranted to anticipate them.

### Why operator-by-hand and not Terraform-managed

Putting the key value in Terraform either commits it to source control (immediately disqualifying) or stores it in the Terraform state file. The state file lives on the operator's laptop at v0 (per ADR 0015's local-state decision); committing high-value credentials to a laptop-resident state file is unsafe.

External secret-injection systems (External Secrets Operator, SOPS-encrypted YAML in git, sealed-secrets, HashiCorp Vault) are appropriate at scale but introduce operational substrate that v0 does not need. For one operator placing two credentials per project, the cost-benefit favors `aws ssm put-parameter` once per key.

### When to revisit secret automation

Three triggers, any of which justifies adopting a heavier solution:

- **Multiple environments.** A dev/staging/prod split needs the same secret under multiple paths or rotated independently per environment. Manual placement scales linearly; managed rotation does not.
- **Frequent rotation.** Compliance-driven rotation (90-day, 30-day) or incident-driven rotation (suspected leak, departing teammate) past quarterly cadence. Manual rotation costs more time than automation costs to set up.
- **Multiple operators.** A second operator turns manual placement into a coordination bottleneck; either every operator holds the credentials in their own environment (unsafe) or a managed system distributes them (the appropriate answer at that point).

None of these is true today.

### Why forward-only with additive discipline, not paired up/down

Paired up/down migrations are the textbook answer to schema rollback, and they work cleanly for pure structural changes (add column ↔ drop column, add index ↔ drop index). Two real-world considerations push toward the forward-only-with-additive discipline instead:

1. **Most rollback needs are satisfied without reversing schema.** When an older binary runs against an additive newer schema, the unused fields are inert. The deploy workflow already supports redeploying any historical SHA in seconds. Rolling back the binary is the *primary* rollback mechanism; the schema typically does not need to move backward at all.
2. **The cases where schema does need to move backward are exactly the cases where paired down migrations are unreliable.** Destructive steps (drop column, rename, narrow constraint) and data transformations (backfill, normalize, split values) have reverses that are either lossy, untested against production-shaped data, or both. For these, snapshot restore is more reliable than a down migration exercised only against fresh-DB fixtures.

The expand-and-contract discipline removes the need for paired migrations by ensuring the rollback case is covered by the additive pattern itself. The cost is bounded: a destructive change requires two deploys instead of one, and expand and contract steps never share a release. The benefit is a single, simple migrator with no rollback machinery to maintain.

### When fix-forward, when snapshot restore

The mental model for which recovery path to take when a migration is regretted:

- **Bad binary, additive schema:** redeploy an older SHA. Schema unchanged, no data lost. Fastest path.
- **Regretted contract migration:** snapshot restore (per ADR 0019), then redeploy the SHA whose code matches the pre-contract schema. Writes between snapshot and restore are lost.
- **Regretted data-transformation migration:** snapshot restore. Forward-correcting the data is also possible if the original values were preserved elsewhere; otherwise the snapshot is the source of truth.
- **Migration violated the additive discipline by mistake:** treat as a regretted contract migration. The code-review process that admitted it is itself audited.

### When to revisit migration tooling

- **Pre-deploy automatic snapshots.** The daily snapshot policy bounds rollback loss to roughly a day. When that is insufficient — typically when recent writes cannot be reconstituted in hours of operator work — add a deploy-time snapshot step before `migrate-db`. The deploy workflow gains an `aws ec2 create-snapshot` call, and rollback granularity moves to the moments before the failed deploy.
- **Paired up/down migrations.** Worth reconsidering when migration cadence grows large enough that the additive discipline becomes harder to maintain than the rollback machinery, or when a multi-environment promotion model needs to land schema changes in pre-prod and back them out without snapshot-restore.
- **Online migrations / zero-downtime migrations.** Locking patterns, dual-write windows, backfill jobs. The right answer when the service has user-facing uptime SLOs. Not relevant at v0.

## Alternatives considered

### Secrets in environment variables baked into the systemd unit

Rejected. The unit file is plain text on disk and visible via `systemctl cat`; secrets in unit files leak to anyone with SSM session access. Even if access is operator-only, this is the wrong primitive.

### Secrets fetched from a vault sidecar (Vault Agent, AWS Secrets Manager sidecar)

Deferred. Appropriate when rotation matters; the right shape, but premature substrate for a single operator with two credentials.

### Secrets in a `.env.production` file deployed via the tarball

Rejected. Couples secret rotation to a release. Encryption of the file requires key management that just moves the problem. Plaintext is disqualifying.

### Migrations applied by a separate tool (Alembic, Flyway, golang-migrate, Liquibase)

Rejected for v0. The existing `apply_migrations()` runner is plain-SQL, plain-Python, fits inside the orchestrator's package, and works. Switching to a dedicated migration framework earns its keep at scale (multi-environment promotion, advanced features like baseline-from-existing-DB); at v0 it adds dependency surface without solving a real problem. Revisit if migration complexity grows past what plain SQL handles cleanly.

### Online schema migrations (zero-downtime, lock-aware)

Deferred. v0 has no uptime SLO; brief unavailability during migration is acceptable. The systemd unit restarts the service after migrations land, so request handling resumes within seconds. Revisit when user-facing uptime requirements emerge.

## Consequences

- **Easier:** Adding a credential to production is one `aws ssm put-parameter` invocation and one IAM-path addition (if a new path is needed). No CI configuration, no GitHub Secrets, no rotation tooling to set up.
- **Easier:** Recovering from a bad release is fast: the deploy workflow accepts an older SHA, ships it through the same path, host restarts. Recovery from a bad migration is slower (snapshot restore + redeploy) but documented.
- **Easier:** The orchestrator's `load_config()` interface is unchanged — secrets arrive via process environment regardless of how they got there.
- **Harder:** Rotation cadence depends on operator discipline. There is no automatic rotation surface; rotation happens when the operator explicitly performs it.
- **Harder:** Code review carries the additive-migration discipline. A non-additive migration that slips through can break rollback safety for future releases until the next snapshot ages out. A migration-review checklist (additive? if not, is it the contract step of a previous expand?) belongs in the PR template when migrations become routine.
- **Harder:** A destructive change requires two deploys instead of one — the "expand" deploy and a later "contract" deploy. The operator commits to never collapsing the two.
- **Accepted commitment:** Out-of-band secrets are a documented operator responsibility. The recovery procedure for a host whose secrets were never seeded is an explicit operator action (`aws ssm put-parameter`), not an automatic substrate behavior.
- **Accepted commitment:** A migration's correctness against production data shapes is the migration author's responsibility. Additive-by-default discipline is the load-bearing convention; snapshot restore is the safety net.

## Deferred

- **AWS Secrets Manager adoption** (when rotation, audit, or multi-environment requirements justify $0.40/secret/month and migration effort).
- **External Secrets Operator / SOPS / Vault sidecar** (when the team grows or secret count grows).
- **Pre-deploy snapshot trigger** (when bounded daily rollback granularity is no longer acceptable).
- **Paired up/down migrations** (when cadence or multi-environment promotion makes them worth the maintenance cost).
- **Online migration tooling** (when an uptime SLO appears).
