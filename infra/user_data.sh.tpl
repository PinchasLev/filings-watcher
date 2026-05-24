#!/bin/bash
# First-boot provisioning for the filings-watcher v0 host.
# Runs once at instance launch via cloud-init (logs at /var/log/cloud-init-output.log).

set -euxo pipefail

# --- security patches ---
dnf update -y

# --- operator tools ---
# sqlite CLI for ad-hoc inspection of /var/lib/filings-watcher/filings.db via
# Session Manager or SSM run-command. The Python orchestrator uses sqlite3
# through the stdlib and does not require the CLI; this is purely for operators.
dnf install -y sqlite

# --- 2 GB swap file ---
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# --- ongoing security updates ---
dnf install -y dnf-automatic
systemctl enable --now dnf-automatic.timer

# --- journald retention: cap at 1 GB on disk, 30 days max ---
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/retention.conf <<'EOF'
[Journal]
SystemMaxUse=1G
MaxRetentionSec=30day
EOF
systemctl restart systemd-journald

# --- SSH hardening: key-only, no root login ---
cat > /etc/ssh/sshd_config.d/10-filings-hardening.conf <<'EOF'
PasswordAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
EOF
systemctl restart sshd

# --- application user ---
if ! id ${app_user} >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash ${app_user}
fi

# --- uv (Python package + toolchain manager) installed for the app user.
#     Python 3.13 is downloaded by uv on first `uv sync` rather than via dnf,
#     keeping toolchain version under uv's control per ADR 0004.
APP_USER_HOME=$(getent passwd ${app_user} | cut -d: -f6)
if [ ! -x "$APP_USER_HOME/.local/bin/uv" ]; then
  sudo -u ${app_user} -H bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi

# --- data volume: attach, format-if-blank, mount at /data ---
# Dedicated EBS volume holding SQLite + Caddy ACME state across instance
# replacements (see ADR 0019). AWS attaches at /dev/sdh; on Nitro this
# surfaces as /dev/nvme*n1, with the numeric suffix non-deterministic.
# The reliable identifier is the EBS volume serial number.

DATA_VOLUME_ID='${data_volume_id}'
DATA_VOLUME_ID_NO_DASHES="$${DATA_VOLUME_ID//-/}"
DATA_DEVICE_LINK="/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_$${DATA_VOLUME_ID_NO_DASHES}"

# Wait up to 60s for the volume attachment to complete and the by-id symlink to appear.
for attempt in $(seq 1 30); do
  if [ -b "$DATA_DEVICE_LINK" ]; then break; fi
  sleep 2
done
if [ ! -b "$DATA_DEVICE_LINK" ]; then
  echo "data volume device $DATA_DEVICE_LINK did not appear after 60s" >&2
  exit 1
fi
DATA_DEVICE=$(readlink -f "$DATA_DEVICE_LINK")

# Format only if no filesystem present — on a re-attached volume, this is
# the critical guard against destroying production data.
if ! blkid "$DATA_DEVICE" >/dev/null 2>&1; then
  mkfs.ext4 -L filings-data "$DATA_DEVICE"
fi

mkdir -p /data
DATA_UUID=$(blkid -s UUID -o value "$DATA_DEVICE")
if ! grep -q "UUID=$DATA_UUID" /etc/fstab; then
  echo "UUID=$DATA_UUID /data ext4 defaults,nofail 0 2" >> /etc/fstab
fi
mountpoint -q /data || mount /data

# --- application directories ---
# /opt tree holds release artifacts (rebuilt by the deploy pipeline) and
# stays on the root volume. /var/lib/filings-watcher holds the SQLite DB
# and is symlinked onto the data volume so it survives instance replacement.
install -d -o ${app_user} -g ${app_user} -m 0755 /opt/filings-watcher
install -d -o ${app_user} -g ${app_user} -m 0755 /opt/filings-watcher/releases
install -d -o ${app_user} -g ${app_user} -m 0755 /opt/filings-watcher/bin
install -d -o ${app_user} -g ${app_user} -m 0755 /data/filings-watcher
ln -sfn /data/filings-watcher /var/lib/filings-watcher

# --- empty SQLite DB so filings-server.service can start before the
#     orchestrator creates the schema. Conditional: leave an existing DB
#     alone (re-attached data volume scenario). SQLite treats a 0-byte
#     file as a fresh DB; the service's /health endpoint doesn't touch tables.
if [ ! -e /var/lib/filings-watcher/filings.db ]; then
  install -o ${app_user} -g ${app_user} -m 0644 /dev/null /var/lib/filings-watcher/filings.db
fi

# --- filings-server systemd unit. The binary lives at
#     /opt/filings-watcher/current/filings-server, where `current` is a
#     symlink pointing at the active release directory under releases/.
cat > /etc/systemd/system/filings-server.service <<'UNIT_EOF'
[Unit]
Description=filings-watcher Go HTTP service
Documentation=https://github.com/PinchasLev/filings-watcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${app_user}
Group=${app_user}
ExecStart=/opt/filings-watcher/current/filings-server
Restart=on-failure
RestartSec=5s

Environment=FILINGS_DB_PATH=/var/lib/filings-watcher/filings.db
# Bind on all interfaces (not just loopback) so operators on the
# tailnet can reach the service directly at http://filings-watcher-host:8080/.
# Inbound access is gated by the EC2 security group, which restricts
# to the Tailscale subnet per ADR 0014 — defence in depth via the SG,
# not via a loopback bind. Future public exposure of selected routes
# goes through Caddy on 443 with TLS.
Environment=LISTEN_ADDR=:8080

# OpenTelemetry. Same vocabulary as the orchestrator wrapper; the
# host-local Collector receives OTLP on 127.0.0.1:4317. service.version
# is omitted intentionally — the Go service rarely changes and a
# dynamic-SHA wrapper is overkill for it.
Environment=OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317
Environment=OTEL_EXPORTER_OTLP_PROTOCOL=grpc
Environment=OTEL_SERVICE_NAME=filings-server
Environment=OTEL_RESOURCE_ATTRIBUTES=service.namespace=filings-watcher

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/var/lib/filings-watcher
StandardOutput=journal
StandardError=journal
SyslogIdentifier=filings-server

LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
UNIT_EOF

systemctl daemon-reload
# Enable for future reboots; do NOT start now — the binary doesn't exist
# until the first deploy lands a release.
systemctl enable filings-server.service

# --- Tailscale daemon (operator runs `tailscale up --ssh` post-provision via SSM) ---
curl -fsSL https://pkgs.tailscale.com/stable/amazon-linux/2023/tailscale.repo \
  -o /etc/yum.repos.d/tailscale.repo
dnf install -y tailscale
systemctl enable --now tailscaled

# --- Caddy (TLS-terminating web server with Let's Encrypt auto-renewal) ---
# Caddy's official RPM packaging covers Fedora via COPR and Debian/Ubuntu via
# Cloudsmith; neither cleanly covers Amazon Linux 2023 on aarch64. We install
# the official static binary and a hand-rolled systemd unit instead. Upgrade
# by bumping CADDY_VERSION and re-applying.
CADDY_VERSION=2.11.3
curl -fsSL -o /tmp/caddy.tar.gz \
  "https://github.com/caddyserver/caddy/releases/download/v$${CADDY_VERSION}/caddy_$${CADDY_VERSION}_linux_arm64.tar.gz"
tar -xzf /tmp/caddy.tar.gz -C /usr/local/bin caddy
chmod 0755 /usr/local/bin/caddy
rm -f /tmp/caddy.tar.gz

if ! id caddy >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir /var/lib/caddy --shell /usr/sbin/nologin caddy
fi
install -d -o caddy -g caddy -m 0755 /etc/caddy
# Caddy ACME state lives on the data volume so issued certs and the account
# key survive instance replacement (preserves Let's Encrypt rate-limit headroom).
install -d -o caddy -g caddy -m 0700 /data/caddy
ln -sfn /data/caddy /var/lib/caddy

cat > /etc/caddy/Caddyfile <<'CADDYFILE_EOF'
{
    email ${acme_email}
}

staging.filingsradar.com {
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "strict-origin-when-cross-origin"
        Permissions-Policy "geolocation=(), microphone=(), camera=()"
    }
    reverse_proxy 127.0.0.1:8080
}
CADDYFILE_EOF
chown caddy:caddy /etc/caddy/Caddyfile
chmod 0644 /etc/caddy/Caddyfile

cat > /etc/systemd/system/caddy.service <<'UNIT_EOF'
[Unit]
Description=Caddy
Documentation=https://caddyserver.com/docs/
After=network.target network-online.target
Requires=network-online.target

[Service]
Type=notify
User=caddy
Group=caddy
ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576
PrivateTmp=true
ProtectSystem=full
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
UNIT_EOF

systemctl daemon-reload
systemctl enable --now caddy

echo "$(date -Iseconds) provisioning complete" > /var/log/filings-provision-complete
