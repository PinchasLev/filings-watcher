# Infrastructure

Terraform configuration for the filings-watcher v0 deploy substrate. Single AWS account, single region (`us-east-1`), local state.

See [ADR 0014](../docs/decisions/0014-operator-access-via-mesh-vpn.md) for the operator-access model and [ADR 0015](../docs/decisions/0015-deploy-pipeline-and-iac-for-v0.md) for the deploy/IaC decisions.

## What this provisions

**Phase 4 slices 1 + 2** (current):

- One EC2 host (`t4g.small`, ARM, Amazon Linux 2023) in `us-east-1a`
- Elastic IP attached to the host (public address survives instance replacement)
- Security group with **no public ingress** — host is reachable only via Tailscale and AWS SSM Session Manager
- IAM role on the instance with `AmazonSSMManagedInstanceCore`
- First-boot provisioning via `user_data.sh.tpl`: security patches, 2 GB swap, automatic updates, journald retention, SSH hardening, `filings` application user, `/opt/filings-watcher/` directory tree, Tailscale daemon installed and enabled (not yet joined to the tailnet — operator does that post-provision)

Subsequent slices will add: Caddy + Route53 (slice 3), the S3 artifact bucket and GitHub OIDC role (slice 4), CloudWatch alarms (slice 7).

## Prerequisites

- [Terraform CLI](https://developer.hashicorp.com/terraform/install) ≥ 1.6 (CI uses 1.15.3)
- AWS CLI configured with credentials for the target account
- Session Manager plugin for AWS CLI ([install](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html))
- A Tailscale account and a tailnet the operator owns

## One-time setup

```bash
cd infra/
terraform init
```

All variables have defaults. Create `terraform.tfvars` only if you want to override anything (see `terraform.tfvars.example`).

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

## If something breaks

- **Tailscale daemon didn't install or isn't running:** SSM in, `journalctl -u tailscaled` and `cat /var/log/cloud-init-output.log`.
- **Can't reach the host from your tailnet:** check `tailscale status` on the host (via SSM) — it should show the device as online.
- **Lost both SSM and Tailscale:** there is no public ingress fallback. Recovery is `terraform taint aws_instance.host && terraform apply` (replaces the instance, then re-bootstrap). The break-glass story is deferred per [ADR 0014](../docs/decisions/0014-operator-access-via-mesh-vpn.md).

## Tearing down

```bash
terraform destroy
```

Releases the EIP, terminates the instance, removes the security group and IAM role. **No data backups are made** — anything on the EBS volume is lost. Remove the host from the tailnet manually via the Tailscale admin console (it'll show as offline after the instance is gone).

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
| Tailscale (free tier: 3 users, 100 devices) | $0 |
| **Total (steady state)** | **~$14/month** |

This sits inside the $20/month budget alarm. Adding Caddy (slice 3) does not change AWS costs.
