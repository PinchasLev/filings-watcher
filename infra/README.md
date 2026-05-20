# Infrastructure

Terraform configuration for the filings-watcher v0 deploy substrate. Single AWS account, single region (`us-east-1`), local state.

See [ADR 0014](../docs/decisions/0014-operator-access-via-mesh-vpn.md) for the operator-access model and [ADR 0015](../docs/decisions/0015-deploy-pipeline-and-iac-for-v0.md) for the deploy/IaC decisions.

## What this provisions

- One EC2 host (`t4g.small`, ARM, Amazon Linux 2023) in `us-east-1a`
- Elastic IP attached to the host (public address survives instance replacement)
- Security group: public ingress on 80/tcp + 443/tcp for Caddy; operator access via Tailscale and AWS SSM Session Manager
- IAM role on the instance with `AmazonSSMManagedInstanceCore`, `s3:GetObject` on the artifact bucket, and `ssm:GetParameter*` on `/filings-watcher/*` (third-party API credentials)
- Route53 A record `staging.filingsradar.com` → the EIP, plus a CAA record locking TLS issuance to Let's Encrypt
- Dedicated EBS data volume (`filings-watcher-data`, 10 GB gp3, encrypted) attached to the host, mounted at `/data`. Application state — SQLite DB and Caddy ACME state — lives on this volume so it survives instance replacement. See [ADR 0019](../docs/decisions/0019-data-persistence-across-instance-replacement.md).
- AWS Data Lifecycle Manager policy taking daily snapshots of the data volume at 06:00 UTC with 7-day retention
- S3 bucket `filingsradar-artifacts` (versioned, encrypted, public access blocked, 90-day current / 30-day noncurrent lifecycle) holding release tarballs
- GitHub OIDC provider plus two IAM roles for GitHub Actions: build (write `releases/*` on push to main) and deploy (invoke the `filings-deploy` SSM document, gated by the `aws-deploy` GitHub environment)
- SSM document `filings-deploy` encapsulating the host-side deploy procedure (S3 pull, tar extract, symlink swap, systemctl restart, health check)
- First-boot provisioning via `user_data.sh.tpl`: security patches, 2 GB swap, automatic updates, journald retention, SSH hardening, `filings` application user, `uv` (Python package and toolchain manager) installed for the app user, `/opt/filings-watcher/` release directory tree, data-volume mount + filesystem bootstrap, `/var/lib/filings-watcher` and `/var/lib/caddy` symlinked into `/data`, empty SQLite DB at `/var/lib/filings-watcher/filings.db` (only when starting from a blank data volume), `filings-server.service` systemd unit installed and enabled, Tailscale daemon installed (operator joins post-provision), Caddy installed and configured to reverse-proxy `staging.filingsradar.com` to `127.0.0.1:8080`

Application code (Go service binary, Python orchestrator) is delivered separately by the deploy pipeline, not by Terraform.

## Prerequisites

- [Terraform CLI](https://developer.hashicorp.com/terraform/install) ≥ 1.6 (CI uses 1.15.3)
- AWS CLI configured with credentials for the target account
- Session Manager plugin for AWS CLI ([install](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html))
- A Tailscale account and a tailnet the operator owns

## One-time setup

```bash
cd infra/
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars: set acme_email to a real address you read.
terraform init
```

The only required input is `acme_email` — Let's Encrypt registers it for ACME notifications (cert expiry warnings, renewal failures). All other variables have sensible defaults.

The provider lockfile (`.terraform.lock.hcl`) is committed; `terraform init` reuses pinned versions.

## Standard workflow

```bash
terraform plan       # preview the change set
terraform apply      # apply (will prompt for confirmation)
```

After `apply`, outputs include:

- `instance_id` — the EC2 instance ID (used in the SSM command)
- `public_ip` — the EIP attached to the host
- `ssm_session_command` — copy-paste command to open a Session Manager shell on the host

## Bootstrapping the host onto the tailnet

The host comes up with Tailscale installed but not joined. To join:

```bash
# Open an SSM session on the host (no inbound network needed)
aws ssm start-session --target <instance-id> --region us-east-1

# Inside the SSM session:
sudo tailscale up --ssh
```

`tailscale up --ssh` prints a URL. Open it on any device already on your tailnet (your laptop's browser is easiest), confirm "Connect," and the host joins. The `--ssh` flag enables Tailscale-mediated SSH so you don't need a public port 22 ever again.

Verify from an operator device on the tailnet:

```bash
tailscale status                              # host should appear with a 100.x.y.z address
tailscale ping <host-magicdns-name>
tailscale ssh ec2-user@<host-magicdns-name>   # opens a shell as ec2-user
```

The host's MagicDNS name is whatever the OS reported during `tailscale up` (default: `ip-x-x-x-x` or similar — rename via `sudo tailscale set --hostname=...` if you want).

## Verifying the public web surface

After apply finishes and the instance has been up for ~2 minutes (cloud-init + Caddy's first ACME request):

```bash
dig staging.filingsradar.com A +short        # should return the EIP
curl -I https://staging.filingsradar.com/    # valid Let's Encrypt cert
```

`/` returns `502 Bad Gateway` from Caddy until a Go service binary has been deployed. The Caddy + TLS layer is working; the upstream isn't running yet.

## One-time AWS configuration

Three values must be seeded out-of-band into AWS Systems Manager Parameter Store before the orchestrator can run. The operator places these once per AWS account; the host fetches them at deploy time via the IAM-scoped read policy (see [ADR 0020](../docs/decisions/0020-secrets-and-migration-rollback.md) for the rationale). `EDGAR_USER_AGENT` must include a contact email per [SEC EDGAR access guidelines](https://www.sec.gov/os/accessing-edgar-data).

```bash
aws ssm put-parameter \
  --name /filings-watcher/anthropic-api-key \
  --value "<your-anthropic-api-key>" \
  --type SecureString \
  --region us-east-1

aws ssm put-parameter \
  --name /filings-watcher/langsmith-api-key \
  --value "<your-langsmith-api-key>" \
  --type SecureString \
  --region us-east-1

aws ssm put-parameter \
  --name /filings-watcher/edgar-user-agent \
  --value "<name> <contact-email>" \
  --type SecureString \
  --region us-east-1
```

Rotation: re-run `put-parameter` with `--overwrite`. The next deploy or orchestrator invocation picks up the new value; no host-side change required.

## Automated deploys (push + click)

Once per repository / AWS account pair, the following GitHub-side configuration must exist before the workflows can run:

1. **Repository variable** `AWS_ACCOUNT_ID` set to the target AWS account number (Settings → Secrets and variables → Actions → Variables).
2. **GitHub Environment** named `aws-deploy` (Settings → Environments). Configure it with required reviewers (at minimum the operator) so each deploy needs explicit human approval before the OIDC role can be assumed. Optionally restrict deployment branches to `main`.

After that one-time setup:

- Pushes to `main` trigger the `publish-artifact` job in CI. After lint and test jobs pass, the binary is built, tarred, and uploaded to `s3://filingsradar-artifacts/releases/<sha>/release.tar.gz` via the OIDC-scoped build role.
- Deploys are operator-triggered: GitHub UI → Actions → "deploy" workflow → "Run workflow" → optional `sha` input (blank = current branch HEAD). The environment gate prompts the configured reviewers; after approval, the workflow invokes the `filings-deploy` SSM document, waits for completion, and smoke-tests `https://staging.filingsradar.com/health`.

Rollback is the same workflow with an older SHA in the `sha` input.

## Running the orchestrator manually

A separate SSM document — `filings-orchestrate-once` — invokes a single classification pass for one filing. Use it to produce the first real classifications before a scheduled cadence is in place, or to verify the pipeline against a specific ticker on demand.

```bash
aws ssm send-command \
  --document-name filings-orchestrate-once \
  --parameters "ticker=AAPL,filingIndex=0" \
  --targets "Key=tag:Name,Values=filings-watcher-host" \
  --region us-east-1
```

The document fetches the API keys from Parameter Store at invocation time, runs `classify-filing` against the requested ticker and filing index (0 = most recent), and persists the classification to the SQLite DB on the data volume. Inspect the result via `https://staging.filingsradar.com/filings` once the pass completes.

## Scheduled ingest (daily-index → classify every 15 minutes)

A second SSM document — `filings-install-orchestrate-timer` — installs a systemd timer on the host that fires `scan-daily-index` every 15 minutes (per [ADR 0012](../docs/decisions/0012-ingestion-cadence-periodic-v0-push-v1.md) and [ADR 0021](../docs/decisions/0021-realtime-8k-ingest-via-daily-index.md)). Run it once per host, after the first release has been deployed:

```bash
aws ssm send-command \
  --document-name filings-install-orchestrate-timer \
  --targets "Key=tag:Name,Values=filings-watcher-host" \
  --region us-east-1
```

The document installs three artifacts: a wrapper script at `/usr/local/bin/filings-orchestrate-tick` that fetches credentials from Parameter Store and execs the CLI; a `filings-orchestrate.service` oneshot unit (12-minute timeout, lock-protected against overlap); and a `filings-orchestrate.timer` unit that schedules the service every 15 minutes after the previous invocation exits. The install fires one invocation explicitly so the next is scheduled 15 minutes after it completes.

Operator-facing commands once installed:

```bash
# Status (from a Session Manager shell on the host)
systemctl status filings-orchestrate.timer
systemctl list-timers filings-orchestrate.timer

# Run once now (does not affect the timer schedule)
sudo systemctl start filings-orchestrate.service

# Inspect output of the most recent invocation
journalctl -u filings-orchestrate.service -n 200 --no-pager
journalctl -u filings-orchestrate.service -f -o cat       # live tail, plain output

# Pause / resume the schedule
sudo systemctl stop filings-orchestrate.timer
sudo systemctl start filings-orchestrate.timer
```

The unit logs JSON-line structured events per [ADR 0013](../docs/decisions/0013-operational-observability-for-v0.md): `tick_started`, `filing_fetched`, `classification_started`, `classification_completed`, `cursor_advanced`, `rate_limited`, `tick_failed`, `tick_completed`.

### Structured-log views with jq

The events are JSON-line per [ADR 0013](../docs/decisions/0013-operational-observability-for-v0.md), so `jq` can project columns from the journal. A few recipes worth keeping:

```bash
# Compact event timeline: event name + accession (if any)
journalctl -u filings-orchestrate.service -o json --no-pager \
  | jq -r '.MESSAGE | fromjson | "\(.event)\t\(.accession_number // "-")"'

# Just the last invocation's events
journalctl -u filings-orchestrate.service -o json --no-pager --since "30 minutes ago" \
  | jq -r '.MESSAGE | fromjson | "\(.ts)  \(.event)  \(.accession_number // "")"'

# Count outcomes across all runs since the last reboot
journalctl -u filings-orchestrate.service -o json --no-pager -b \
  | jq -r '.MESSAGE | fromjson | .event' | sort | uniq -c

# Find every rate_limited backoff, with provider and backoff duration
journalctl -u filings-orchestrate.service -o json --no-pager \
  | jq 'select(.MESSAGE | fromjson | .event == "rate_limited") | .MESSAGE | fromjson'

# Aggregate per-tick: duration and new-filings-count
journalctl -u filings-orchestrate.service -o json --no-pager \
  | jq -r '.MESSAGE | fromjson | select(.event == "tick_completed") | "\(.ts)  duration_ms=\(.duration_ms)  new_filings=\(.new_filings_count)"'
```

The `MESSAGE | fromjson` step is the key piece: journald wraps each line of the unit's stdout in its own JSON envelope, and the orchestrator's structured event lives in the inner `MESSAGE` string. Parsing it twice gives access to the typed fields.

## OpenTelemetry Collector (foundation, verification-only)

Per [ADR 0018](../docs/decisions/0018-observability-otel-native-operator-controlled.md), the host runs an OpenTelemetry Collector (Contrib distribution) as the first hop for all telemetry. The version is pinned via the `otel_collector_version` Terraform variable so bumps are an SSM rerun, not a code change.

### Install or upgrade

Run the SSM document (idempotent — safe to re-run, and re-runs pick up any new config changes or version bumps):

```bash
aws ssm send-command \
  --document-name filings-install-otel-collector \
  --targets "Key=instanceIds,Values=$(terraform output -raw instance_id)" \
  --region us-east-1
```

Track the run:

```bash
aws ssm list-command-invocations --command-id <CMD-ID> --details --region us-east-1
```

### Verification (no backend or dashboard required)

The initial install ships with two exporters whose purpose is local verification, not user-facing display:

- **`debug` exporter** — prints every received metric, span, and log to the Collector's stdout, captured by journald.
- **`prometheus` exporter** — exposes incoming metrics as Prometheus exposition format on `127.0.0.1:8889`.

From the host:

```bash
# Is the Collector running and healthy?
sudo systemctl status otelcol-contrib

# Live view of arrivals (will be empty until apps are instrumented)
sudo journalctl -u otelcol-contrib -f -o cat

# Scrape the local Prometheus endpoint
curl -s http://127.0.0.1:8889/metrics | head -40
```

From the operator laptop on the tailnet (via SSH tunnel, since the Prometheus port is bound to localhost only by default):

```bash
ssh -L 8889:localhost:8889 filings-watcher-host
# in a second terminal:
curl -s http://localhost:8889/metrics
```

Until the orchestrator and Go service are instrumented (subsequent observability PRs), the `/metrics` output will be sparse — only the Collector's own self-telemetry. That's expected: this install is the substrate, not a deliverable surface.

### Logs to CloudWatch (durable backend)

After the journald receiver and the `awscloudwatchlogs` exporter are wired into the Collector config (re-run the install SSM doc to apply the updated config), the orchestrator's structured events flow to a CloudWatch Logs group at `/filings-watcher/orchestrator`. The exporter auto-creates the group on first write; the host's IAM role is scoped to `/filings-watcher/*` so the Collector cannot write into unrelated streams.

**Stream events live (from operator laptop, no SSH required):**

```bash
aws logs tail /filings-watcher/orchestrator --follow --region us-east-1
```

**Insights queries** — because the `json_parser` operator lifts every field of our structured event into the LogRecord's attributes alongside the journald envelope (`_HOSTNAME`, `_SYSTEMD_UNIT`, `_PID`, ...), envelope and app fields are co-queryable at the same flat level:

```text
fields @timestamp, event, accession_number, duration_ms, _SYSTEMD_UNIT
| filter event = "tick_completed"
| sort @timestamp desc
| limit 50
```

```text
fields @timestamp, event, message
| filter event = "tick_failed"
| sort @timestamp desc
```

```text
stats count() by event
| sort count desc
```

CloudWatch console: `Logs > Log groups > /filings-watcher/orchestrator > Search log group`.

### Where traces, metrics, and richer dashboards land

Not in this install. App-side OTel SDK instrumentation (Python orchestrator and Go service) lands in subsequent PRs and starts pushing spans + metrics through the same Collector pipelines. Dashboards (CloudWatch Dashboards or a swappable backend per ADR 0018) come after instrumentation produces signals worth visualizing.

## Manual deploy (bootstrap and break-glass)

The standard deploy path is automated via GitHub Actions, S3, and SSM. The procedure below is the bootstrap used to land the first binary on a fresh host, and the break-glass path when the automated pipeline is unavailable.

### From the operator laptop

```bash
# Cross-compile a static linux/arm64 binary
cd ~/projects/filings-watcher/service
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o /tmp/filings-server ./cmd/filings-server

# Send it to the host via taildrop. Files land in /var/lib/tailscale/files/<sender>/,
# which is root-owned on the receiving host.
tailscale file cp /tmp/filings-server filings-watcher-host:
```

### On the host

Open a session — either `aws ssm start-session --target <instance-id> --region us-east-1` or `tailscale ssh ec2-user@filings-watcher-host` — then run:

```bash
# Retrieve the file from the taildrop inbox into /tmp/. `--conflict=overwrite`
# replaces any prior file with the same name from previous deploys.
sudo tailscale file get --conflict=overwrite /tmp/

# Sanity check the binary architecture (taildrop dir is root-only, so this needs sudo too)
sudo file /tmp/filings-server
# Expect: ELF 64-bit LSB executable, ARM aarch64, statically linked

# Install, swap the symlink, start
sudo install -d -o filings -g filings -m 0755 /opt/filings-watcher/releases/manual-bootstrap
sudo install -o filings -g filings -m 0755 /tmp/filings-server /opt/filings-watcher/releases/manual-bootstrap/filings-server
sudo ln -sfn /opt/filings-watcher/releases/manual-bootstrap /opt/filings-watcher/current
sudo systemctl start filings-server
sudo systemctl status filings-server --no-pager | head -15
```

### Verify (from anywhere)

```bash
curl -sS https://staging.filingsradar.com/health
# Expect: {"status":"ok"}
```

If `/health` returns the JSON status, the runtime layer is wired correctly end-to-end (Caddy → Go service → systemd → SQLite).

## If something breaks

- **Tailscale daemon didn't install or isn't running:** SSM in, `journalctl -u tailscaled` and `cat /var/log/cloud-init-output.log`.
- **Can't reach the host from your tailnet:** check `tailscale status` on the host (via SSM) — it should show the device as online.
- **Cert not issued / browser warning:** Caddy retries ACME on a backoff. SSM in and check `journalctl -u caddy`. Common causes: DNS not propagated yet (wait, or `dig +trace`), port 80 blocked upstream, ACME rate limit hit (5 duplicate certs / week per domain — try `staging2.filingsradar.com` to recover).
- **CAA record blocking issuance:** if you change the issuance CA from Let's Encrypt, update `aws_route53_record.caa` to match before applying.
- **`/` returns 502 Bad Gateway:** Caddy is up but the Go service isn't. Check `systemctl status filings-server` and `journalctl -u filings-server` via SSM. Most common cause on a fresh host: no binary has been deployed yet (run the manual deploy procedure above).
- **`filings-server` won't start:** check the env (`FILINGS_DB_PATH` must point at an existing file), check that `/opt/filings-watcher/current` is a valid symlink to a directory containing `filings-server`, check the binary architecture matches the host (`file /opt/filings-watcher/current/filings-server` should say `aarch64`).
- **Lost both SSM and Tailscale:** there is no public ingress fallback for shell access. Recovery is `terraform taint aws_instance.host && terraform apply` (replaces the instance, then re-bootstrap). The break-glass story is deferred per [ADR 0014](../docs/decisions/0014-operator-access-via-mesh-vpn.md).

## Recovering from a corrupted or accidentally-deleted file

The data volume is snapshotted daily; recovery uses an AWS snapshot restore. From the operator laptop:

```bash
# 1. Find the most recent good snapshot
aws ec2 describe-snapshots \
  --owner-ids self \
  --filters "Name=tag:Name,Values=filings-watcher-data" \
  --query 'Snapshots[*].[SnapshotId,StartTime,State]' \
  --output table

# 2. Create a new volume from the chosen snapshot (in the same AZ as the host)
aws ec2 create-volume \
  --snapshot-id snap-xxxxxxxx \
  --availability-zone us-east-1a \
  --volume-type gp3 \
  --encrypted \
  --tag-specifications 'ResourceType=volume,Tags=[{Key=Name,Value=filings-watcher-data-restore}]'

# 3. Stop the service, detach the current volume, attach the restored one
#    (do this via SSM session on the host; commands omitted here for brevity)
#    Then update infra/data_volume.tf with the new volume ID and run `terraform apply`.
```

A full procedure (with the host-side mount swap) is documented when first exercised in anger.

## Tearing down

```bash
terraform destroy
```

Releases the EIP, terminates the instance, removes the security group and IAM role.

**The data volume is preserved by default.** `aws_ebs_volume.data` has `lifecycle { prevent_destroy = true }` to guard against accidental loss. Removing the data volume requires a deliberate two-step operator action: remove the lifecycle block from `data_volume.tf`, then `terraform destroy` (or `terraform destroy -target=aws_ebs_volume.data`). Snapshots are retained per the DLM policy regardless.

Remove the host from the tailnet manually via the Tailscale admin console (it'll show as offline after the instance is gone).

## State management

State is stored locally in `infra/terraform.tfstate`. This file is gitignored. Back it up if you make destructive changes; if lost, AWS resources can be re-imported via `terraform import`.

Migration to S3 + DynamoDB remote state is deferred until a second engineer joins, CI runs `terraform plan`, or the laptop-as-state-store risk becomes operationally concerning. See [ADR 0015](../docs/decisions/0015-deploy-pipeline-and-iac-for-v0.md) "Deferred" section.

## Cost expectations (us-east-1, on-demand)

| Resource | Approx. monthly cost |
|---|---|
| `t4g.small` (730 hrs/month) | ~$12.27 |
| 20 GB gp3 root volume | ~$1.60 |
| 10 GB gp3 data volume | ~$0.80 |
| EBS snapshots (7-day rolling, ~10 GB) | ~$0.50 |
| Elastic IP (attached) | $0 |
| Elastic IP (detached, e.g., during instance replacement) | ~$3.65/mo if held |
| Data transfer out (first 100 GB/month free) | $0 at v0 traffic |
| Tailscale (free tier: 3 users, 100 devices) | $0 |
| Route53 hosted zone (already provisioned) | ~$0.50 |
| Let's Encrypt certificates | $0 |
| S3 artifact storage (rolling releases, well under 1 GB) | ~$0.05 |
| **Total (steady state)** | **~$15.75/month** |

This sits inside the $20/month budget alarm.
