# 0019. Data persistence across instance replacement

- **Status:** Accepted
- **Date:** 2026-05-18

## Context

The v0 substrate provisions one EC2 host with a root EBS volume that is destroyed on instance termination. [ADR 0015](0015-deploy-pipeline-and-iac-for-v0.md) commits `user_data_replace_on_change = true`, meaning any change to first-boot provisioning forces an instance replacement. Several substrate changes during phase 4 have already triggered replacements (Tailscale install, Caddy install, systemd unit addition); more are expected as the substrate evolves.

Application data presently lives on the root volume:

- The SQLite database at `/var/lib/filings-watcher/filings.db`, into which the orchestrator (Python) writes filings and classifications and out of which the service (Go) reads.
- Caddy's ACME state at `/var/lib/caddy`, holding the Let's Encrypt account key and the issued certificate plus its renewal metadata.

On every instance replacement, both are destroyed. The database starts empty (no real data has been written yet) and Caddy re-acquires a certificate from Let's Encrypt on the new host. This is acceptable today; it stops being acceptable the moment the orchestrator begins writing real classifications, and it becomes operationally painful long before that — Let's Encrypt enforces a duplicate-certificate rate limit of 5 issuances per 7 days per registered domain, which heavy substrate iteration can exhaust.

The forcing function: real data starts flowing when the orchestrator's first scheduled run lands. Before that point, durability must be a property of the substrate, not a property of the operator remembering to back up before each `terraform apply`.

## Decision

A dedicated EBS volume holds all data that must survive instance replacement. The volume is provisioned by Terraform with `delete_on_termination = false` on the attachment, lives across instance lifecycle, and is attached to whichever instance is the current host. Daily snapshots are taken automatically via AWS Data Lifecycle Manager.

Concretely:

- **`aws_ebs_volume "data"`**: 10 GB gp3, encrypted at rest, tagged `Name=filings-watcher-data` and `Snapshot=daily`.
- **`aws_volume_attachment "data"`**: attaches the volume to the current host at the AWS device name `/dev/sdh`. On Nitro instances this surfaces as an `/dev/nvme*n1` device; user_data discovers the actual device by EBS volume serial number rather than hard-coded path.
- **Filesystem**: `ext4`. First-boot user_data formats the volume only if it is blank (no filesystem detected), so an existing volume's data is preserved across instance replacement.
- **Mount point**: `/data`. Two symlinks point production state at it: `/var/lib/filings-watcher` → `/data/filings-watcher` and `/var/lib/caddy` → `/data/caddy`. Single mount, single fstab entry, single recovery story.
- **AWS Data Lifecycle Manager policy**: takes a snapshot daily at 06:00 UTC (before US market open, after typical scheduled runs), retains the seven most recent snapshots, scoped by resource tag `Snapshot=daily`. Snapshots are encrypted with the same key as the source volume.
- **Cross-region snapshot copy**: not configured. The single-region constraint matches the rest of the substrate; cross-region DR is deferred until there is a product reason to invest in it.

## Rationale

### Why a separate EBS volume, not "back up before each apply"

Operator discipline is the worst place to keep durability requirements. Every replacement event becomes a chance to forget; every recovery becomes "did I take the backup or not?" A separate volume with its own lifecycle makes the durability property structural — the operator can `terraform apply` substrate changes freely without thinking about data loss.

The cost is small: 10 GB gp3 at ~$0.80/month plus a few cents for snapshot storage. Inside the existing $20/month budget without contention.

### Why include Caddy's ACME state on the data volume

Caddy stores its Let's Encrypt account key and the issued certificate at `/var/lib/caddy`. If this is on the root volume, every instance replacement returns the host to a brand-new ACME registration — which works, but consumes one of Let's Encrypt's 5-per-7-days duplicate-certificate issuances per registered name. Heavy substrate iteration during a single week can exhaust that quota and leave the host serving an expired certificate.

Putting `/var/lib/caddy` on the data volume turns instance replacement into a zero-cost event from Let's Encrypt's perspective: the existing account key, cached cert, and renewal schedule survive. The rate-limit headroom is preserved for real renewals and recoveries.

### Why ext4

`ext4` is the boring, well-supported default for general-purpose Linux storage. AL2023's `xfs` default for the root volume is reasonable for OS workloads but offers no advantage for our use case (small SQLite database, small Caddy state directory). `ext4` toolchain availability is uniformly excellent across recovery scenarios — `e2fsck`, `resize2fs`, `dumpe2fs`, and standard rescue-image tooling all assume it.

### Why mount at `/data` with symlinks rather than direct mount points

Two ways to surface a single volume into multiple application paths:

1. **Bind mounts**: mount the volume once at `/data`, then bind-mount `/data/filings-watcher` onto `/var/lib/filings-watcher` and `/data/caddy` onto `/var/lib/caddy`. Two fstab entries beyond the single mount, three mount operations at boot.
2. **Symlinks**: mount the volume at `/data`, replace `/var/lib/filings-watcher` and `/var/lib/caddy` with symlinks pointing at the corresponding subdirectories.

Symlinks win on simplicity: one fstab entry, two `ln -sfn` operations, no nested mount semantics, no "is this filesystem mounted?" questions when troubleshooting. The applications see the same paths they always did (`/var/lib/...`) and read/write transparently across the symlink. Recovery via `dd` of the volume image is also simpler — the symlink targets are subdirectories in a normal filesystem, not bind-mount entries.

### Why discover the device by serial number, not hard-code the path

The AWS device name attached at `/dev/sdh` does not consistently appear at `/dev/sdh` on the host. On AL2023 Nitro instances the EBS volume surfaces as `/dev/nvme*n1`, with the numeric suffix dependent on attachment order and other factors. Hard-coding `/dev/sdh` in fstab will not work; hard-coding `/dev/nvme1n1` will work today but is fragile against attachment-order changes.

The reliable identifier is the EBS volume's serial number, which appears as `/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol-<id>`. user_data discovers the actual block device via this symlink and writes the fstab entry against the by-id path.

### Why daily snapshots with 7-day retention

Backup cadence is a function of two values: how much data are you willing to lose, and how much storage are you willing to pay for. For the v0 workload:

- Filings + classifications are derived from public EDGAR data. Losing the most recent day's classifications means re-running the orchestrator's classification step on the affected period — the source filings themselves are not lost.
- Caddy ACME state is recoverable from Let's Encrypt directly (with the rate-limit caveat already discussed).

Daily snapshots match this risk profile: a day's loss is recoverable in hours of orchestrator work; sub-daily granularity is overkill. Seven-day retention covers the "I noticed corruption a week late" recovery window without retaining material storage cost.

### Why no cross-region snapshot copy at v0

Cross-region snapshot copy doubles snapshot storage cost and adds a cross-region data-transfer charge per snapshot. The benefit — surviving a regional AWS outage — is real but not high-probability and is mitigated, for a public-data project, by the fact that nothing on the data volume is irrecoverable in principle (filings re-fetch from EDGAR; certs re-issue from Let's Encrypt). The trigger to revisit is either a product reason that makes the data harder to reconstitute or a regulatory reason that requires geographic redundancy.

## Alternatives considered

### Keep data on the root volume; take a manual backup before each instance replacement

Rejected. Relies on operator discipline at the worst possible moment (in the middle of an infrastructure change). Every replacement is an opportunity to forget. Recovery from a forgotten backup is data loss, not friction.

### Switch SQLite to RDS or another managed database service

Rejected for v0 — already covered in [ADR 0008](0008-sqlite-for-v0-persistence.md). Managed databases solve persistence the way this ADR solves it (state outside the compute lifecycle), but at substantially higher operational and financial cost. SQLite plus a durable volume gets the same durability property at v0 scale without the managed-DB tax.

### EFS instead of EBS

Rejected. EFS provides shared-filesystem semantics across multiple instances, which v0 does not need (single-host substrate, single writer). EFS pricing model (per-GB plus throughput) is more expensive than EBS gp3 at our access pattern, and SQLite over NFS has well-known correctness considerations. Right tool for a different shape; wrong tool here.

### Self-managed snapshots via a host-side cron

Rejected. AWS Data Lifecycle Manager is the AWS-native answer: declarative policy, no host-side script to maintain, IAM-scoped, encryption handled. The cron path adds operational surface (a script to write, a failure mode to monitor, a credential management story for the cron to assume AWS permissions) for no benefit.

### Continuous database-level backup (e.g., Litestream replicating SQLite to S3)

Deferred. Litestream is an interesting fit for SQLite specifically — it replicates the WAL to S3 continuously, giving point-in-time recovery rather than daily snapshots. It is the right next step when the data volume snapshot policy is no longer sufficient (transactions worth losing approaches "even one"). For v0, daily snapshots of a database whose source data is publicly re-fetchable is enough. Revisit when the orchestrator writes data that cannot be reconstituted from public sources.

## Consequences

- **Easier:** Substrate changes that trigger instance replacement (security group adjustments, daemon installs, systemd unit changes) no longer threaten application data. The operator applies infrastructure changes without thinking about backup.
- **Easier:** Caddy's ACME state survives replacement; the Let's Encrypt rate-limit budget is preserved for renewals and emergencies.
- **Easier:** Recovery from a corrupted DB or a deleted-by-accident file is a snapshot restore: `aws ec2 describe-snapshots`, `aws ec2 create-volume --snapshot-id`, swap attachment.
- **Easier:** Disaster recovery (lost AWS account region, lost host) has a documented procedure: detach volume, attach to a new instance, mount, done.
- **Harder:** One more EBS resource and one DLM policy to maintain. Operational surface increase is modest; both are declarative Terraform.
- **Harder:** First-boot user_data is more complex — it must detect whether the data volume is freshly created (needs formatting) or pre-existing (already has a filesystem and should be mounted as-is without data loss). Wrong logic here destroys data.
- **Accepted commitment:** The data volume's lifecycle is managed separately from the instance's. `terraform destroy` of the host does *not* destroy the data volume. Decommissioning the project requires an explicit additional step to remove the data volume.
- **Accepted commitment:** Snapshots accrue cost over time, even at 7-day retention. Cost is small (a few cents per month at v0 data volume) but non-zero and monitored as part of the AWS spend envelope.

## Recovery procedure

Documented in `infra/README.md` alongside the standard workflow. The summary: a corrupted or accidentally-deleted file is recovered by restoring the most recent good snapshot to a new EBS volume, detaching the broken volume, attaching the restored volume, mounting it, and restarting the affected services. No host-side restoration scripts are needed; the procedure is a sequence of AWS API calls.

## Deferred

- **Continuous WAL replication for SQLite (Litestream or similar).** Revisit when daily-grain loss is no longer acceptable — typically when transactions represent value that cannot be reconstituted from public sources.
- **Cross-region snapshot copy.** Revisit when regional-outage durability becomes a product requirement.
- **Volume resizing automation.** The 10 GB starting size is generous for v0; manual resize via `aws ec2 modify-volume` + `resize2fs` is sufficient when the time comes. Revisit if growth becomes frequent enough to justify automation.
- **Multi-host shared storage.** The single-host substrate makes EBS (single-attach) the right choice. The v2 multi-host trigger is the right time to revisit; possible answers at that point include EFS, S3-backed object storage, or a managed database depending on the access pattern.
