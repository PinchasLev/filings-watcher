# Infrastructure

Terraform configuration for the filings-watcher v0 deploy substrate. Single AWS account, single region (`us-east-1`), local state.

See [ADR 0015](../docs/decisions/0015-deploy-pipeline-and-iac-for-v0.md) for the architectural decisions captured here.

## What this provisions

**Phase 4 slice 1 — host provisioning** (this slice):

- One EC2 host (`t4g.small`, ARM, Amazon Linux 2023) in `us-east-1a`
- Elastic IP attached to the host (public address survives instance replacement)
- Security group locked to SSH from the operator's IP only
- IAM role on the instance with `AmazonSSMManagedInstanceCore` (SSM Session Manager as fallback access)
- First-boot provisioning via `user_data.sh.tpl`: security patches, 2 GB swap, automatic updates, journald retention, SSH hardening, `filings` application user, `/opt/filings-watcher/` directory tree

Subsequent slices will add: Tailscale (slice 2), Caddy + Route53 (slice 3), the S3 artifact bucket and GitHub OIDC role (slice 4), CloudWatch alarms (slice 7).

## Prerequisites

- [Terraform CLI](https://developer.hashicorp.com/terraform/install) ≥ 1.6
- AWS CLI configured with credentials for the target account
- An ed25519 SSH key pair on the operator machine (`~/.ssh/id_ed25519` / `id_ed25519.pub`)

## One-time setup

```bash
cd infra/
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars: set operator_ip (your current public IP /32) and ssh_public_key.
terraform init
```

The provider lockfile (`.terraform.lock.hcl`) is committed; `terraform init` reuses pinned versions.

## Standard workflow

```bash
terraform plan       # preview the change set
terraform apply      # apply (will prompt for confirmation)
```

After `apply`, the outputs include:

- `public_ip` — the EIP attached to the host
- `ssh_command` — copy-paste SSH command (you'll wait ~2 min for first-boot provisioning to finish)
- `ssm_session_command` — fallback SSM Session Manager command if SSH ever breaks

## Verifying the host

```bash
# SSH access (after ~2 min for cloud-init to complete)
ssh ec2-user@<public_ip>

# Verify first-boot provisioning completed
ssh ec2-user@<public_ip> 'cat /var/log/filings-provision-complete'
ssh ec2-user@<public_ip> 'sudo -u filings whoami'
ssh ec2-user@<public_ip> 'ls -la /opt/filings-watcher'
```

If SSH fails, use SSM Session Manager via the `ssm_session_command` output to investigate.

## Updating the operator IP

Residential IPs change. When yours does:

```bash
# Update operator_ip in terraform.tfvars
terraform apply
```

Only the security group rule changes; the instance is not touched.

## Tearing down

```bash
terraform destroy
```

Releases the EIP, terminates the instance, removes the security group and IAM role. **No data backups are made** — anything on the instance's EBS volume is lost.

## State management

State is stored locally in `infra/terraform.tfstate`. This file is gitignored. Back it up if you make destructive changes; if lost, AWS resources can be re-imported via `terraform import`.

Migration to S3 + DynamoDB remote state is deferred until a second engineer joins, CI runs `terraform plan`, or the laptop-as-state-store risk becomes operationally concerning. See [ADR 0015](../docs/decisions/0015-deploy-pipeline-and-iac-for-v0.md) "Deferred" section.

## Cost expectations (us-east-1, on-demand)

| Resource | Approx. monthly cost |
|---|---|
| `t4g.small` (730 hrs/month) | ~$12.27 |
| 20 GB gp3 root volume | ~$1.60 |
| Elastic IP (attached) | $0 |
| Elastic IP (detached, e.g., during instance replacement) | ~$3.65/mo if held |
| Data transfer out (first 100 GB/month free) | $0 at v0 traffic |
| **Total (steady state)** | **~$14/month** |

This sits inside the $20/month budget alarm. Adding Tailscale (slice 2) and Caddy (slice 3) does not change AWS costs.
