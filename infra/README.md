# Infrastructure

Terraform configuration for the filings-watcher v0 deploy substrate. Single AWS account, single region (`us-east-1`), local state.

See [ADR 0014](../docs/decisions/0014-operator-access-via-mesh-vpn.md) for the operator-access model and [ADR 0015](../docs/decisions/0015-deploy-pipeline-and-iac-for-v0.md) for the deploy/IaC decisions.

## What this provisions

- One EC2 host (`t4g.small`, ARM, Amazon Linux 2023) in `us-east-1a`
- Elastic IP attached to the host (public address survives instance replacement)
- Security group: public ingress on 80/tcp + 443/tcp for Caddy; operator access via Tailscale and AWS SSM Session Manager
- IAM role on the instance with `AmazonSSMManagedInstanceCore`
- Route53 A record `staging.filingsradar.com` → the EIP, plus a CAA record locking TLS issuance to Let's Encrypt
- First-boot provisioning via `user_data.sh.tpl`: security patches, 2 GB swap, automatic updates, journald retention, SSH hardening, `filings` application user, `/opt/filings-watcher/` directory tree, empty SQLite DB at `/var/lib/filings-watcher/filings.db`, `filings-server.service` systemd unit installed and enabled, Tailscale daemon installed (operator joins post-provision), Caddy installed and configured to reverse-proxy `staging.filingsradar.com` to `127.0.0.1:8080`

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

## Manual deploy (bootstrap and break-glass)

The standard deploy path is automated via GitHub Actions, S3, and SSM. The procedure below is the bootstrap used to land the first binary on a fresh host, and the break-glass path when the automated pipeline is unavailable.

From the operator laptop (with `tailscale` connected and the host visible in `tailscale status`):

```bash
# 1. Cross-compile a static linux/arm64 binary
cd ~/projects/filings-watcher/service
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o /tmp/filings-server ./cmd/filings-server

# 2. Ship it to the host
tailscale file cp /tmp/filings-server filings-watcher-host:
# or: scp via tailscale ssh
#   scp /tmp/filings-server filings-watcher-host:/tmp/filings-server

# 3. Install it, swap the symlink, start the service
tailscale ssh ec2-user@filings-watcher-host <<'REMOTE_EOF'
set -euo pipefail
sudo install -d -o filings -g filings -m 0755 /opt/filings-watcher/releases/manual-bootstrap
sudo install -o filings -g filings -m 0755 /tmp/filings-server /opt/filings-watcher/releases/manual-bootstrap/filings-server
sudo ln -sfn /opt/filings-watcher/releases/manual-bootstrap /opt/filings-watcher/current
sudo systemctl start filings-server
sudo systemctl status filings-server --no-pager | head -15
REMOTE_EOF
```

Verify from anywhere:

```bash
curl -sS https://staging.filingsradar.com/health   # {"status":"ok"}
```

If `/health` returns the JSON status, the runtime layer is wired correctly end-to-end (Caddy → Go service → systemd → SQLite stub file).

## If something breaks

- **Tailscale daemon didn't install or isn't running:** SSM in, `journalctl -u tailscaled` and `cat /var/log/cloud-init-output.log`.
- **Can't reach the host from your tailnet:** check `tailscale status` on the host (via SSM) — it should show the device as online.
- **Cert not issued / browser warning:** Caddy retries ACME on a backoff. SSM in and check `journalctl -u caddy`. Common causes: DNS not propagated yet (wait, or `dig +trace`), port 80 blocked upstream, ACME rate limit hit (5 duplicate certs / week per domain — try `staging2.filingsradar.com` to recover).
- **CAA record blocking issuance:** if you change the issuance CA from Let's Encrypt, update `aws_route53_record.caa` to match before applying.
- **`/` returns 502 Bad Gateway:** Caddy is up but the Go service isn't. Check `systemctl status filings-server` and `journalctl -u filings-server` via SSM. Most common cause on a fresh host: no binary has been deployed yet (run the manual deploy procedure above).
- **`filings-server` won't start:** check the env (`FILINGS_DB_PATH` must point at an existing file), check that `/opt/filings-watcher/current` is a valid symlink to a directory containing `filings-server`, check the binary architecture matches the host (`file /opt/filings-watcher/current/filings-server` should say `aarch64`).
- **Lost both SSM and Tailscale:** there is no public ingress fallback for shell access. Recovery is `terraform taint aws_instance.host && terraform apply` (replaces the instance, then re-bootstrap). The break-glass story is deferred per [ADR 0014](../docs/decisions/0014-operator-access-via-mesh-vpn.md).

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
| Route53 hosted zone (already provisioned) | ~$0.50 |
| Let's Encrypt certificates | $0 |
| **Total (steady state)** | **~$14.50/month** |

This sits inside the $20/month budget alarm.
