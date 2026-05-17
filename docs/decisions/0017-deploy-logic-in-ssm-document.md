# 0017. Deploy logic lives in the SSM document, not a host-side script

- **Status:** Accepted
- **Date:** 2026-05-17

## Context

[ADR 0015](0015-deploy-pipeline-and-iac-for-v0.md) commits to the deploy pipeline shape (GitHub Actions → S3 → workflow_dispatch → AWS OIDC → SSM Send-Command) and specifies the host-side action as:

> The host script downloads the tarball from S3, stages it to `/opt/filings-watcher/releases/<sha>/`, swaps the `/opt/filings-watcher/current` symlink atomically, and runs `systemctl restart filings-server orchestrator`.

That commitment leaves an implementation question open: *where does the host script live, and how does it get there?* Three viable answers:

1. Inside `user_data.sh.tpl`, baked onto the host at provision time.
2. Inside the release tarball, written to disk on every deploy.
3. Inside the SSM document body, executed by the SSM agent directly.

Each option has a different change-management story for the deploy logic itself. The deploy logic is small (≈15 lines of shell), rarely changes, and is operationally critical — when it has a bug, deploys fail.

## Decision

The deploy logic lives in the `filings-deploy` SSM document, defined in `infra/ssm_deploy.tf`. There is no host-side `pull-and-deploy.sh` and no script in the release tarball. The SSM agent on the host receives the document body as runtime commands and executes them.

The document accepts one parameter, `sha`, validated with `allowedPattern = "^[0-9a-f]{7,40}$"`. The body downloads `s3://filingsradar-artifacts/releases/<sha>/release.tar.gz`, extracts to `/opt/filings-watcher/releases/<sha>/`, atomically swaps `/opt/filings-watcher/current`, restarts `filings-server`, and verifies the unit is active.

The GitHub Actions deploy workflow invokes the document by name and SHA; the IAM trust policy on the deploy role is scoped to this single document (`arn:aws:ssm:...:document/filings-deploy`).

## Rationale

### Bootstrap problem with option 2 (script in tarball)

A script-in-tarball deploy mechanism cannot extract its own tarball. Some out-of-band action — user_data, a previous deploy, manual intervention — has to land an initial version of the script. Option 3 has no bootstrap problem: the SSM document exists in AWS the moment Terraform creates it, and the very first deploy uses the same code path as every subsequent deploy.

### Iteration cost with option 1 (script in user_data)

Anything in `user_data.sh.tpl` is part of the EC2 instance's identity. Changes to user_data force a `user_data_replace_on_change` replacement of the entire host — TLS state, application state, Tailscale identity, all destroyed and rebuilt. Iterating on a deploy script that lives in user_data therefore costs an instance replacement per change. For a script that may need quick fixes when a deploy breaks, that cost is prohibitive.

### Source-of-truth coherence

Option 3 keeps the deploy procedure version-controlled alongside the rest of the infrastructure (`infra/ssm_deploy.tf`), in Terraform. Iteration is `terraform apply`, no instance churn, no release artifact churn. The application repo holds application code; infra holds infra. Deploy procedure is unambiguously infra.

### Embedded shell in JSON: bearable

The drawback of option 3 is that the shell body is embedded as a JSON-escaped array of strings inside `jsonencode()`. At ≈15 lines, this is mildly ugly but readable. If the procedure grows substantially (say, multi-step health checks, blue/green steps, post-deploy smoke tests beyond `systemctl is-active`), the right move is to split logic into multiple `mainSteps` rather than letting one shell block balloon. Terraform's `jsonencode` handles the escaping; no manual quoting required.

### Why not a separate script file on the host installed by Terraform via SSM Send-Command?

Possible — Terraform could on first apply send the script to the host via SSM and write it to `/opt/filings-watcher/bin/`. That adds a second imperative step to `terraform apply`, introduces drift if the file is edited out of band, and reintroduces a host-side artifact that the rest of the architecture avoids. Worse than embedding the body in the SSM document.

## Alternatives considered

### Script in `user_data.sh.tpl`

Rejected. Forces instance replacement on every deploy-logic change. Deploy logic changes more often than user_data should.

### Script in release tarball

Rejected. Bootstrap problem on first deploy. Mixes infra concerns (deploy procedure) into application releases. Iteration requires a release-only-for-script-change.

### CodeDeploy

Rejected. CodeDeploy is the AWS-managed answer for this shape of problem and is a reasonable choice at larger scale. The v0 cost (extra service to learn, configure, and maintain; appspec.yml lifecycle hooks layered on top of our own systemd unit) exceeds the benefit when one operator owns one host. The SSM Send-Command path uses primitives already in the substrate (IAM, SSM agent, S3) without adding a new managed service.

### Ansible / Chef / Puppet

Rejected. Configuration-management tools are designed for inventory-driven, multi-host fleets. Their inventory and connection layers are overhead at single-host scale. The v0 substrate is intentionally light.

## Consequences

- **Easier:** No host-side script to maintain. Deploy logic is in source control with the rest of the infra, in HCL the rest of the project already speaks.
- **Easier:** Bug fixes to the deploy procedure are `terraform apply`, not a release. Recovery from a broken deploy doesn't require a working deploy.
- **Easier:** Auditability: every deploy is `ssm:SendCommand` invoking a named document, recorded in CloudTrail with the parameters used.
- **Harder:** Adding multi-step orchestration (e.g., blue/green, canary, multi-host coordination) past a certain point will outgrow a single SSM document. The migration target at that point is CodeDeploy or a workflow-engine-driven deploy.
- **Accepted commitment:** The release tarball stays purely artifactual. It contains compiled binaries, Python source trees, configuration templates — but no scripts that mutate host state. Host-state mutation lives in IaC.

## Relation to ADR 0015

This ADR refines the implementation detail [ADR 0015](0015-deploy-pipeline-and-iac-for-v0.md) left open. The pipeline shape (GitHub Actions → S3 → SSM) is unchanged; the host-side execution mechanism is now an SSM document body rather than a tarball-shipped or user_data-baked shell script. ADR 0015's reference to `/opt/filings-watcher/bin/pull-and-deploy.sh` is superseded by the `filings-deploy` SSM document.

## Deferred

- **Multi-step deploy orchestration.** Health-check probes after restart, automated rollback on failed health check, blue/green or canary release patterns. Revisit when the unit-failure CloudWatch alarm (slice 7) is in place — that becomes the first feedback loop for deciding whether a richer deploy story earns its weight.
- **Migration to CodeDeploy.** Revisit when the substrate becomes multi-host or when the deploy procedure stops fitting in a single SSM document.
