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

echo "$(date -Iseconds) slice-1 provisioning complete" > /var/log/filings-provision-complete
