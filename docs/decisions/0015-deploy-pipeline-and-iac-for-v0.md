# 0015. Deploy pipeline and IaC for v0

- **Status:** Accepted
- **Date:** 2026-05-15

## Context

v0 needs to ship from this repository to a running AWS host. Four coupled decisions force themselves at once:

1. **Artifact format.** What does "a release" look like — a container image, a binary, a source tree?
2. **Supply chain security.** Which scanning gates run on every release, and what severity policy blocks merge?
3. **Deploy pipeline.** How do build outputs get from CI to the host, and what credentials does that movement need?
4. **Infrastructure provisioning.** Is the host (and its supporting AWS resources) defined as code, or clicked into existence?

Each decision has a spectrum from "lightweight, fits the current single-host scope" to "heavy, fits a much larger eventual scope." Choosing the heavy end prematurely costs operational surface that v0 cannot service; choosing the light end without writing down the migration triggers leaves future-you guessing at why each choice was made.

The constraints are stable across this ADR: single-host deploy ([ADR 0005](0005-compute-architecture-read-mostly-and-async-workers.md), [ADR 0008](0008-sqlite-for-v0-persistence.md), [ADR 0014](0014-operator-access-via-mesh-vpn.md)), single operator ([ADR 0013](0013-operational-observability-for-v0.md)), $20/month AWS budget. v1 stays single-host; v2 (multi-host) is hypothetical and triggered separately.

## Decision

### Artifact format: native binaries and source tree, not container images

Each release produces two artifacts:

- A statically-compiled Go binary (`filings-server`) built from `service/` via `just build`.
- The Python orchestrator as a source tree plus `uv.lock` for reproducible dependency resolution.

These are bundled into a single tarball named by commit SHA: `release-<sha>.tar.gz`.

Code is written **container-ready but not containerized**. The following are enforceable commitments, checked on every PR review:

- All configuration via environment variables or a config file path passed via env var. No hardcoded paths in source.
- Database, log, and config paths are external inputs ([ADR 0008](0008-sqlite-for-v0-persistence.md) committed this; this ADR enforces it).
- Logs emit to stdout/stderr; capture is the runtime's responsibility (`systemd-journald` at v0).
- The build entry point is `just build`; the runtime entry point is the built binary or `uv run`, with no additional bootstrap.
- Processes exit cleanly on SIGTERM.

Wrapping these in a Dockerfile later is an afternoon's work, triggered by moving off single-host.

### Supply chain and artifact security

Every release passes scanning gates configured as GitHub Actions workflows. All chosen tools are GitHub-native or first-party to the language ecosystem; zero monthly cost at v0 scale.

**Dependency vulnerability scanning (on every PR):**

- **Python**: `pip-audit` against the resolved `uv.lock`.
- **Go**: `govulncheck` against the module graph.
- **Both**: GitHub Dependabot opens PRs for vulnerable dependencies; those PRs go through normal review.

**Severity policy.** Critical and High CVEs block merge **unless explicitly exempted** in a `.security-exceptions.yml` file at the repository root. An exemption requires:

- The specific CVE ID and affected dependency.
- The reason no upstream fix is available (no patch released, package abandoned, vulnerable code path not invoked by our code).
- A compensating control or rationale (behind authentication, scheduled for replacement, mitigated by configuration).
- An expiry date — default 30 days; longer requires re-justification.

Medium and Low findings carry a 30-day triage SLA without blocking merge.

**Secret scanning:**

- The existing `scan-secrets` workflow remains in place.
- GitHub's native push protection is enabled at the repository level (rejects commits containing recognized secret patterns).

**Static analysis (SAST):**

- GitHub CodeQL runs on every PR and on a weekly schedule. Findings surface in the repository's Security tab.

**Dependency posture:**

- All dependencies pinned via `uv.lock` and `go.sum`.
- Dependabot version-bump PRs are reviewed individually, not auto-merged. The reviewer judges the diff (release notes, scope of change, signal-to-noise of the dependency tree), not just the version delta. A compromised dependency at a patch-version bump can pass CI; the only defense against that class of attack is a human pass over the dependency change itself.

What is **not** part of v0:

- Container image scanning (no images to scan; coupled to the containerization trigger).
- SBOM generation (deferred; becoming standard but not yet load-bearing for a single-developer project).
- License compliance scanning (project is MIT; dependency licenses vetted at intake).
- Runtime exploit monitoring (out of scope for v0).

### Deploy pipeline: GitHub Actions → S3 → manual-trigger → OIDC → SSM

Two GitHub Actions workflows.

**Build** runs on every push and PR:

1. Run tests (`just test`) and lint (`just lint`).
2. Run the scanning gates above.
3. On `main` only: build the Go binary, package the Python tree, tar them as `release-<sha>.tar.gz`.
4. Upload the tarball to `s3://filingsradar-artifacts/releases/<sha>/release.tar.gz`. The bucket has lifecycle rules: 30-day retention on non-current versions, 90-day retention on artifacts not pointed at by `latest`.

**Deploy** runs on `workflow_dispatch` only — the operator clicks a button in the GitHub UI:

1. Inputs: target commit SHA (default: latest `main`).
2. Job assumes an AWS role via GitHub Actions OIDC.
3. Job runs `aws ssm send-command` against the EC2 instance, invoking `/opt/filings-watcher/bin/pull-and-deploy.sh <sha>`.
4. The host script downloads the tarball from S3, stages it to `/opt/filings-watcher/releases/<sha>/`, swaps the `/opt/filings-watcher/current` symlink atomically, and runs `systemctl restart filings-server orchestrator`.

Rollback is the same workflow with an older SHA.

No SSH keys, no long-lived credentials in GitHub Secrets. SSM Send-Command authenticates via the EC2 instance's IAM role and is audited via CloudTrail.

### Infrastructure: Terraform-light, local state, narrow resource scope

The full AWS resource graph for v0 is:

- 1 × EC2 instance (initial: `t4g.small`, ARM, Amazon Linux 2023)
- 1 × Elastic IP (so the public address survives instance replacement)
- 1 × Security group (SSH from operator IP; 80/443 from anywhere; Tailscale port from anywhere; egress unrestricted)
- 2 × Route53 A records (`filingsradar.com` → EIP; `staging.filingsradar.com` → EIP during Phase 4)
- 1 × S3 bucket (artifact storage with lifecycle rules)
- 1 × IAM role for the EC2 instance (SSM agent, S3 read)
- 1 × IAM role for GitHub Actions OIDC (S3 write, `ssm:SendCommand` scoped to the specific instance)
- 1 × CloudWatch alarm (unit-failure, per [ADR 0013](0013-operational-observability-for-v0.md))

Terraform manages all of the above as HCL in `infra/` at the repository root. State lives in **local files** at v0; the migration to S3+DynamoDB state backend is deferred until a second engineer, CI-driven plans, or laptop-loss risk forces it.

## Rationale

### Why no containers at v0

Containers solve problems v0 doesn't have: portability across host environments, isolation in shared infrastructure, immutable runtime artifacts for orchestrators. v0's runtime *is* a single known host running Amazon Linux 2023 — there is nothing to be portable across. SQLite on a local volume actively fights containerization, since the container would need a persistent volume mount and the lifecycle of that mount is now a deploy concern. The Dockerfile would exist to maintain a build path nobody is using. Deferring containerization to its actual trigger (moving off single-host) means writing the Dockerfile once with the migration's real requirements in view, not once now and again later.

### Why no artifact registry

Artifact registries (JFrog Artifactory, Sonatype Nexus, ECR, GHCR) earn their keep when multiple consumers need versioned access to artifacts, when artifacts are distributed across teams or geographies, or when an enterprise scanning workflow ingests them. v0 has one consumer (the single EC2 host), one operator, and no distribution problem. S3 with lifecycle policies is the same storage primitive at a small fraction of the operational surface. The registry decision is properly coupled to the containerization decision: when containers exist, ECR or GHCR becomes the natural store.

### Why GitHub-native and ecosystem-native scanners

`govulncheck`, `pip-audit`, Dependabot, and CodeQL together cover the v0 risk surface (vulnerable dependencies, secret leaks, static-analysis findings) at zero dollar cost, zero infrastructure, and zero ongoing maintenance beyond reviewing findings. They produce alerts in the same surface where code review happens, which keeps remediation inside the developer workflow rather than in a separate tool. Enterprise scanners (Snyk, Veracode, Sonatype IQ, JFrog Xray) earn their cost at scales where the dependency graph is too large for human triage per PR, where compliance reporting requires auditable scan history, or where dedicated security engineers consume findings as their day job. None of these apply to v0. The architectural commitment is "every release is scanned at build time" — the specific tools are swappable when scale or compliance demands.

### Why severity-blocking with exemptions, not hard-blocking

Hard-blocking on every Critical or High CVE creates a failure mode where an unpatched advisory in a transitive dependency stops all merges until the upstream maintainer acts — which can take weeks. Without a documented carve-out, pressure to ship turns into pressure to weaken the policy, and the bar erodes silently. Explicit exemptions with expiry dates convert "we cannot ship" into "we have documented this specific risk, accepted it for a bounded period, and committed to re-evaluating." The audit trail (`git log .security-exceptions.yml`) shows which risks the project knowingly accepted, when, and why. The discipline is preserved precisely because the safety valve is documented and time-boxed.

### Why no Dependabot auto-merge

Patch-version bumps look safe — semantic versioning promises bug fixes only. But supply-chain compromises (event-stream in 2018, ua-parser-js in 2021, the recurring PyPI typosquatting waves, the xz backdoor in 2024) land *inside* package versions. CI runs the compromised code, tests pass, auto-merge ships it, production runs whatever the attacker shipped. The only defense against that class of attack is a human pass over the dependency change itself. At v0's PR volume (a handful per week), the review cost is small; the cost of being wrong is unbounded. The migration to selective auto-merge — typically with a release-cooldown that gives the community time to detect compromises — is triggered when PR volume becomes operationally burdensome.

### Why OIDC + SSM over SSH from CI

SSH from GitHub Actions requires an SSH key in GitHub Secrets. That key is long-lived, has shell access to the host, and is shared across every workflow run. OIDC + SSM substitutes IAM identity for a long-lived secret: GitHub Actions presents a short-lived OIDC token, AWS exchanges it for an IAM role's temporary credentials, and the role can only `ssm:SendCommand` on the specific instance invoking a specific script. There is no shell access, no long-lived credential, and CloudTrail records every invocation by GitHub repository and commit SHA. The configuration cost is modest (an IAM role with a trust policy and one inline policy) and pays back the first time a credential-rotation question comes up.

### Why manual-trigger deploys, not auto-on-merge

Auto-deploy on merge is the right pattern for mature systems with confident test coverage, monitoring that catches regressions in minutes, and rollback that is reliably automatic. v0 has none of those yet. A manual trigger keeps the operator in the loop on every deploy, makes timing explicit (deploy before market open, not while watching a filing land), and removes a class of "merge accidentally broke prod" failure modes. Migrating to auto-deploy when the conditions are met is a workflow YAML edit, not a redesign.

### Why Terraform-light, not CDK or Pulumi

Terraform's HCL is the industry-default IaC syntax and the boring tool for this resource graph. CDK and Pulumi offer programming-language-based abstractions that earn their cost when resource graphs are large, parametric, or repeated. v0's resource graph is approximately 80 lines of HCL describing roughly 10 resources. The abstractions do not compress that meaningfully, and they pull a programming language into the substrate layer where its expressive power is operational risk (loops generating resources, dynamic constructs that complicate state).

### Why local Terraform state at v0

Local state has one risk (laptop loss = state file loss) and one cost (state files are not version-controlled in the conventional sense). Both are bearable at v0: AWS resources persist if state is lost (they can be imported back into a new state file), and a single operator running plans from their laptop does not have the concurrent-edit problem S3+DynamoDB state solves. The migration to remote state is mechanical (`terraform init -migrate-state`) and triggered by a second engineer or CI-driven plans.

## Alternatives considered

### Container images for v0 (Dockerfile + ECR or GHCR)

Rejected. Solves no problem v0 has; introduces a Dockerfile, an image build step, and a registry to maintain. Trigger for revisiting is the move off single-host, where containers become mandatory for ECS Fargate / App Runner / EKS.

### JFrog Artifactory or Sonatype Nexus

Rejected. Designed for many-consumer, security-scanned, license-compliant artifact distribution at organizational scale. v0 has one consumer and no compliance surface. Cost (typically $500+/month for hosted, substantial operational cost for self-hosted) dwarfs the entire v0 budget.

### Enterprise scanning platforms (Snyk, Veracode, Sonatype IQ, JFrog Xray)

Rejected for v0. Designed for organizations with security teams as the consumer; cost ($1K+/month minimum at typical tiers) dwarfs the entire v0 budget; capability beyond GitHub-native and ecosystem tools is marginal at single-developer dependency volume.

### No scanning, defer to runtime detection

Rejected. Supply-chain attacks have become a regular threat surface. The cost of adding Dependabot, `govulncheck`, and `pip-audit` is three config files; the cost of skipping them is unbounded.

### Dependabot auto-merge on patch versions

Rejected for v0. Convenience does not justify the supply-chain risk at this scale. Revisit with release-cooldown semantics when PR volume justifies it.

### AWS CDK

Rejected. Higher abstraction than this resource graph rewards. The 80 lines of HCL would become approximately 150 lines of TypeScript or Python with no expressiveness gain at this size, and the resulting CloudFormation stack adds a deploy substrate (change sets, stack drift detection) that Terraform's plan/apply model avoids.

### Pulumi

Rejected for the same reason as CDK. Comparable abstraction cost without sufficient resource graph to amortize it.

### Manual provisioning via AWS console + documented runbook

Rejected. Click-through provisioning is faster to a first running host (an hour, versus a few hours to write the Terraform). The cost arrives at the first rebuild — a host swap, an AZ change, a region experiment, a clean teardown for cost reasons — when the runbook has to be re-executed step-by-step against memory. Terraform amortizes the upfront investment across every future change.

### SSH-based deploy from GitHub Actions

Rejected as primary mechanism. Defensible alternative — simpler than OIDC + SSM by one moving part — but stores a long-lived SSH key in GitHub Secrets that has shell access to the host. The OIDC + SSM path avoids the credential entirely and provides a stronger audit trail. The simplicity argument does not outweigh the credential-hygiene one.

### Auto-deploy on every merge to main

Deferred, not rejected. Right answer for mature systems with strong test coverage and automatic rollback. v0 does not have either yet. Revisit when operator confidence and observability surface combine to make automatic deploys safer than manual ones.

### AWS CodeDeploy / CodePipeline

Rejected. AWS-native deploy pipeline tooling designed for fleets and elaborate release strategies (blue/green, canary, rolling). The single-host v0 deploy has none of those concerns. The learning curve of CodeDeploy's deployment groups, hook scripts, and CodePipeline's stage modeling does not pay back at this scale.

## Consequences

- **Easier:** Deploys are operator-initiated from any device with browser access to GitHub. Rollback is the same workflow with a different SHA — no special path.
- **Easier:** Vulnerable dependencies surface the day they are disclosed via Dependabot PRs and Security-tab alerts. The alert *is* the work item — no separate tracking system.
- **Easier:** AWS resource teardown and rebuild is `terraform destroy && terraform apply`. Cost-driven experiments (try a smaller instance, test a different AZ) become cheap.
- **Easier:** No long-lived credentials in GitHub Secrets. The IAM trust relationship with GitHub OIDC is the only persistent authentication surface, and it is narrowly scoped to one repository and one instance.
- **Easier:** Artifact storage is one S3 bucket with lifecycle rules. No image build pipeline, no registry to maintain, no signing infrastructure.
- **Harder:** Dependabot PRs are a recurring review obligation. At v0 dependency volume this is manageable; at scale, Dependabot's grouping rules absorb the noise.
- **Harder:** Two systems coordinate on every deploy (GitHub Actions and AWS SSM). Failures can happen on either side; debugging requires looking in both places.
- **Harder:** Terraform state lives on the operator's laptop. Backup discipline (or accepting an import-from-AWS rebuild path) is the operator's responsibility.
- **Harder:** The host's runtime state (SQLite database, journal logs) is not Terraform-managed. Backup and restore for application data is a separate concern with its own runbook.
- **Accepted commitment:** Code stays container-ready by convention. Hardcoded paths, hidden bootstrap steps, or stdout-vs-file-log inconsistencies are rejected on review.
- **Accepted commitment:** Every deploy is an immutable, addressable artifact tagged by commit SHA. Hot-fixes on the host (SSH in, edit, restart) are not acceptable — they produce drift the deploy pipeline cannot reproduce.
- **Accepted commitment:** Critical and High CVEs block merge until remediated or explicitly accepted via an entry in `.security-exceptions.yml` with a compensating control and an expiry date. Lower-severity findings have a 30-day triage SLA.
- **Accepted commitment:** Dependency updates are reviewed before merge, not auto-merged. The reviewer judges the change set, not just the version bump.
- **Accepted commitment:** Anthropic API spend cap (per [ADR 0012](0012-ingestion-cadence-periodic-v0-push-v1.md)) is configured before the first unattended deploy. A deploy that lands new orchestrator code without the cap is incomplete.

## Deferred

- **Containerization.** Triggered by moving off single-host. The natural next stop is ECS Fargate or App Runner, both of which require container images. When triggered, the Dockerfile and a paired container-registry decision (ECR if AWS-only, GHCR if multi-cloud) land in a follow-up ADR.
- **Container registry.** Coupled to containerization. ECR is the default if migration stays inside AWS.
- **Container image scanning** (Trivy, Grype, ECR built-in). Triggered by containerization itself.
- **SBOM generation** (CycloneDX or SPDX format). Increasingly standard for supply-chain transparency and may become compliance-relevant for B2B customers. Triggered by either compliance need or containerization.
- **Dependabot release-cooldown or selective auto-merge** for trusted ecosystems and dev dependencies. Triggered when PR review volume becomes operationally burdensome. The default cooldown (3–7 days post-release) catches most supply-chain compromises while restoring auto-merge convenience.
- **Remote Terraform state (S3 + DynamoDB locking).** Triggered by a second engineer, CI-driven `terraform plan` checks on PRs, or operator preference to remove the laptop-as-state-store risk.
- **Automated deploy on merge to main.** Triggered by sufficient test coverage and observability that automatic deploys become safer than manual ones. The pipeline already produces the immutable artifact; the only change is removing the `workflow_dispatch` gate.
- **`docker-compose` for local development.** Triggered by a second contributor or local-dependency complexity (e.g., adding a Postgres for v2 work). Dockerfiles built for that purpose are reusable when containerization is later triggered for deploy.
- **Multi-AZ or multi-region deployment.** Triggered by uptime SLAs or geographic distribution needs. Out of scope for v0 and v1.
- **Runtime exploit monitoring** (Falco, AWS GuardDuty, etc.). Triggered by a production-criticality threshold v0 does not aspire to.
