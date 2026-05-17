#!/bin/bash
# First-boot provisioning for the filings-watcher v0 host.
# Runs once at instance launch via cloud-init (logs at /var/log/cloud-init-output.log).

set -euxo pipefail

# --- security patches ---
dnf update -y

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

# --- application directories ---
install -d -o ${app_user} -g ${app_user} -m 0755 /opt/filings-watcher
install -d -o ${app_user} -g ${app_user} -m 0755 /opt/filings-watcher/releases
install -d -o ${app_user} -g ${app_user} -m 0755 /opt/filings-watcher/bin
install -d -o ${app_user} -g ${app_user} -m 0755 /var/lib/filings-watcher

# --- empty SQLite DB so filings-server.service can start before the
#     orchestrator creates the schema. SQLite treats a 0-byte file as a
#     fresh DB; the service's /health endpoint doesn't touch tables.
install -o ${app_user} -g ${app_user} -m 0644 /dev/null /var/lib/filings-watcher/filings.db

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
Environment=LISTEN_ADDR=127.0.0.1:8080

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
  useradd --system --create-home --home-dir /var/lib/caddy --shell /usr/sbin/nologin caddy
fi
install -d -o caddy -g caddy -m 0755 /etc/caddy
install -d -o caddy -g caddy -m 0700 /var/lib/caddy

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

echo "$(date -Iseconds) slice-4a provisioning complete" > /var/log/filings-provision-complete
