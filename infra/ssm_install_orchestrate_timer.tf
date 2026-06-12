# SSM document that installs the periodic ingest under systemd: three
# wrapper scripts, three oneshot service units, and three timers — the
# Atom feed (ADR 0029, near-real-time), the daily-index reconciliation
# backstop (ADR 0021, evening cluster per ADR 0029), and the classify
# reconciler that heals orphaned filings (ADR 0030). The operator runs
# this once per host (e.g., after the first deploy on a new instance).
#
# Why an SSM document rather than user_data: both timers depend on the
# orchestrator release tree being present on disk
# (`/opt/filings-watcher/current/orchestrator`), which only exists
# after the first `filings-deploy` SSM run. Installing at first-boot
# would race that ordering. Installing on demand keeps the install
# ordered after the deploy.
#
# Per ADR 0012 and ADR 0029:
#   - Atom feed: OnUnitInactiveSec=30s — schedule next run 30 seconds
#     after the previous exited. OnUnitInactiveSec serializes naturally
#     with the running tick; a tick that legitimately takes longer than
#     30s simply delays the next firing rather than overlapping.
#   - Daily-index: OnCalendar at 22:15, 22:30, 22:45, 23:00
#     America/New_York with Persistent=true. EDGAR publishes the
#     daily-index file once per day around 22:00 ET; the cluster
#     catches publication regardless of routine slippage, and the
#     filings PK makes the redundant invocations free.
#   - Classify reconciler: OnUnitInactiveSec=20m. Heals orphaned
#     filings — a row with no classification (ADR 0030) — by re-running
#     the map stage over stored body text; no EDGAR fetch. Cost-cap
#     gated, idempotent, and continue-on-failure, so it drains a backlog
#     across runs and a run racing a live tick is harmless (the
#     classifications unique index makes any double-work a no-op).
#   - All three: TimeoutStartSec=12m bounds a stuck tick (SIGTERM at 12 min)
#     and flock -n --conflict-exit-code=0 protects against an operator-
#     triggered run racing a scheduled one.
#
# An initial invocation of the Atom service is fired explicitly at the
# end of the install so OnUnitInactiveSec has a reference point —
# without that, the timer would wait forever for the service to have
# been "inactive" at least once. The daily-index timer uses OnCalendar
# and needs no priming; it will fire at the next 22:15 ET window.
#
# Secrets are fetched from SSM Parameter Store inside each wrapper at
# run time, matching the operator-seeded pattern from ADR 0020.

resource "aws_ssm_document" "install_orchestrate_timer" {
  name            = "filings-install-orchestrate-timer"
  document_type   = "Command"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install + enable the systemd timers for scan-atom-feed (30s), scan-daily-index (evening cluster), and reclassify-orphans (20m reconciler)"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "install"
      inputs = {
        runCommand = [
          "set -euo pipefail",
          "if [ ! -d /opt/filings-watcher/current/orchestrator ]; then",
          "  echo \"orchestrator release not present at /opt/filings-watcher/current/orchestrator; deploy a release first\" >&2",
          "  exit 1",
          "fi",
          # --- Cleanup of pre-rename units, if present ---
          # An earlier incarnation of this doc installed a single timer
          # under `filings-orchestrate.{service,timer}` (the daily-index
          # tick on a 15-min cadence). Remove those unit files and stop
          # the timer so the host runs only the new pair below.
          # Idempotent: a fresh host has neither and the if-guard skips.
          "if [ -f /etc/systemd/system/filings-orchestrate.timer ]; then",
          "  systemctl disable --now filings-orchestrate.timer || true",
          "  systemctl stop filings-orchestrate.service || true",
          "  rm -f /etc/systemd/system/filings-orchestrate.timer",
          "  rm -f /etc/systemd/system/filings-orchestrate.service",
          "  rm -f /usr/local/bin/filings-orchestrate-tick",
          "  systemctl daemon-reload",
          "fi",
          # --- Daily-index wrapper ---
          "cat > /usr/local/bin/filings-daily-index-tick <<'TICK_EOF'",
          "#!/bin/bash",
          "# One daily-index reconciliation invocation. Fetches secrets from",
          "# Parameter Store and execs scan-daily-index. Invoked by",
          "# filings-daily-index.service (which wraps this in flock -n).",
          "set -euo pipefail",
          "ANTHROPIC_API_KEY=$(aws ssm get-parameter --name /filings-watcher/anthropic-api-key --with-decryption --query Parameter.Value --output text --region ${var.aws_region})",
          "LANGSMITH_API_KEY=$(aws ssm get-parameter --name /filings-watcher/langsmith-api-key --with-decryption --query Parameter.Value --output text --region ${var.aws_region})",
          "EDGAR_USER_AGENT=$(aws ssm get-parameter --name /filings-watcher/edgar-user-agent --with-decryption --query Parameter.Value --output text --region ${var.aws_region})",
          "export ANTHROPIC_API_KEY LANGSMITH_API_KEY EDGAR_USER_AGENT",
          "export FILINGS_DB_PATH=/var/lib/filings-watcher/filings.db",
          # ADR 0029 spend-cap surface. Operator-tunable starting values;
          # update in source and re-apply terraform + re-run this doc to
          # push to the host. The orchestrator falls back to the same
          # defaults via config.py if these are unset, so an outdated
          # wrapper does not silently disable the cap.
          "export ANTHROPIC_DAILY_COST_CAP_USD=5.00",
          "export ANTHROPIC_DAILY_COST_WARN_USD=4.00",
          # OpenTelemetry — same vocabulary as the Atom wrapper below.
          "RELEASE_SHA=$(basename $(readlink -f /opt/filings-watcher/current))",
          "export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317",
          "export OTEL_EXPORTER_OTLP_PROTOCOL=grpc",
          "export OTEL_SERVICE_NAME=filings-orchestrator",
          "export OTEL_RESOURCE_ATTRIBUTES=service.namespace=filings-watcher,service.version=$RELEASE_SHA",
          "export OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental",
          "export TRACELOOP_TRACE_CONTENT=false",
          "cd /opt/filings-watcher/current/orchestrator",
          "exec /home/filings/.local/bin/uv run --no-sync scan-daily-index",
          "TICK_EOF",
          "chmod 0755 /usr/local/bin/filings-daily-index-tick",
          "chown root:root /usr/local/bin/filings-daily-index-tick",
          # --- Atom-feed wrapper ---
          # Same env shape as the daily-index wrapper (secrets, cost cap,
          # OTel). Differs only in the final exec target: scan-atom-feed
          # instead of scan-daily-index. The CLI itself tags its
          # tick_started / tick_completed / tick_failed events with
          # source=atom_feed so journald and trace queries can separate
          # the two streams.
          "cat > /usr/local/bin/filings-atom-feed-tick <<'TICK_EOF'",
          "#!/bin/bash",
          "set -euo pipefail",
          "ANTHROPIC_API_KEY=$(aws ssm get-parameter --name /filings-watcher/anthropic-api-key --with-decryption --query Parameter.Value --output text --region ${var.aws_region})",
          "LANGSMITH_API_KEY=$(aws ssm get-parameter --name /filings-watcher/langsmith-api-key --with-decryption --query Parameter.Value --output text --region ${var.aws_region})",
          "EDGAR_USER_AGENT=$(aws ssm get-parameter --name /filings-watcher/edgar-user-agent --with-decryption --query Parameter.Value --output text --region ${var.aws_region})",
          "export ANTHROPIC_API_KEY LANGSMITH_API_KEY EDGAR_USER_AGENT",
          "export FILINGS_DB_PATH=/var/lib/filings-watcher/filings.db",
          "export ANTHROPIC_DAILY_COST_CAP_USD=5.00",
          "export ANTHROPIC_DAILY_COST_WARN_USD=4.00",
          "RELEASE_SHA=$(basename $(readlink -f /opt/filings-watcher/current))",
          "export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317",
          "export OTEL_EXPORTER_OTLP_PROTOCOL=grpc",
          "export OTEL_SERVICE_NAME=filings-orchestrator",
          "export OTEL_RESOURCE_ATTRIBUTES=service.namespace=filings-watcher,service.version=$RELEASE_SHA",
          "export OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental",
          "export TRACELOOP_TRACE_CONTENT=false",
          "cd /opt/filings-watcher/current/orchestrator",
          "exec /home/filings/.local/bin/uv run --no-sync scan-atom-feed",
          "TICK_EOF",
          "chmod 0755 /usr/local/bin/filings-atom-feed-tick",
          "chown root:root /usr/local/bin/filings-atom-feed-tick",
          # --- Classify-reconciler wrapper ---
          # Heals orphaned filings (a row with no classification, ADR 0030)
          # by re-running the map stage over stored body text. Needs only the
          # Anthropic credential and the DB path — no EDGAR user agent, because
          # it never fetches from EDGAR (filing text is immutable). The cost cap
          # is exported so a heal shares the same daily budget the live ticks
          # consult and stops cleanly when it is reached.
          "cat > /usr/local/bin/filings-reclassify-orphans-tick <<'TICK_EOF'",
          "#!/bin/bash",
          "set -euo pipefail",
          "ANTHROPIC_API_KEY=$(aws ssm get-parameter --name /filings-watcher/anthropic-api-key --with-decryption --query Parameter.Value --output text --region ${var.aws_region})",
          "export ANTHROPIC_API_KEY",
          "export FILINGS_DB_PATH=/var/lib/filings-watcher/filings.db",
          "export ANTHROPIC_DAILY_COST_CAP_USD=5.00",
          "cd /opt/filings-watcher/current/orchestrator",
          "exec /home/filings/.local/bin/uv run --no-sync reclassify-orphans",
          "TICK_EOF",
          "chmod 0755 /usr/local/bin/filings-reclassify-orphans-tick",
          "chown root:root /usr/local/bin/filings-reclassify-orphans-tick",
          "install -d -o filings -g filings -m 0755 /var/lib/filings-watcher",
          # --- Daily-index service + timer ---
          "cat > /etc/systemd/system/filings-daily-index.service <<'SERVICE_EOF'",
          "[Unit]",
          "Description=filings-watcher daily-index reconciliation (one invocation)",
          "Documentation=https://github.com/PinchasLev/filings-watcher",
          "After=network-online.target",
          "Wants=network-online.target",
          "",
          "[Service]",
          "Type=oneshot",
          "User=filings",
          "Group=filings",
          "TimeoutStartSec=12m",
          "ExecStart=/usr/bin/flock -n --conflict-exit-code=0 /var/lib/filings-watcher/daily-index.lock /usr/local/bin/filings-daily-index-tick",
          "StandardOutput=journal",
          "StandardError=journal",
          "SyslogIdentifier=filings-daily-index",
          "NoNewPrivileges=true",
          "PrivateTmp=true",
          "SERVICE_EOF",
          "cat > /etc/systemd/system/filings-daily-index.timer <<'TIMER_EOF'",
          "[Unit]",
          "Description=Evening cluster invocations of the daily-index reconciliation",
          "Documentation=https://github.com/PinchasLev/filings-watcher",
          "",
          "[Timer]",
          "Unit=filings-daily-index.service",
          "OnCalendar=*-*-* 22:15:00 America/New_York",
          "OnCalendar=*-*-* 22:30:00 America/New_York",
          "OnCalendar=*-*-* 22:45:00 America/New_York",
          "OnCalendar=*-*-* 23:00:00 America/New_York",
          "Persistent=true",
          "",
          "[Install]",
          "WantedBy=timers.target",
          "TIMER_EOF",
          # --- Atom-feed service + timer ---
          "cat > /etc/systemd/system/filings-atom-feed.service <<'SERVICE_EOF'",
          "[Unit]",
          "Description=filings-watcher Atom-feed ingest (one invocation)",
          "Documentation=https://github.com/PinchasLev/filings-watcher",
          "After=network-online.target",
          "Wants=network-online.target",
          "",
          "[Service]",
          "Type=oneshot",
          "User=filings",
          "Group=filings",
          "TimeoutStartSec=12m",
          "ExecStart=/usr/bin/flock -n --conflict-exit-code=0 /var/lib/filings-watcher/atom-feed.lock /usr/local/bin/filings-atom-feed-tick",
          "StandardOutput=journal",
          "StandardError=journal",
          "SyslogIdentifier=filings-atom-feed",
          "NoNewPrivileges=true",
          "PrivateTmp=true",
          "SERVICE_EOF",
          "cat > /etc/systemd/system/filings-atom-feed.timer <<'TIMER_EOF'",
          "[Unit]",
          "Description=Periodic invocation of the Atom-feed ingest",
          "Documentation=https://github.com/PinchasLev/filings-watcher",
          "",
          "[Timer]",
          "Unit=filings-atom-feed.service",
          "OnUnitInactiveSec=30s",
          "",
          "[Install]",
          "WantedBy=timers.target",
          "TIMER_EOF",
          # --- Classify-reconciler service + timer ---
          # OnUnitInactiveSec=20m: schedule the next heal 20 minutes after the
          # previous one exits, so runs never overlap and a large backlog drains
          # across runs rather than stacking. Its own lock file lets it run
          # concurrently with the live ticks (different locks) — safe by the
          # data-layer idempotency, no coordination needed (ADR 0030).
          "cat > /etc/systemd/system/filings-reclassify-orphans.service <<'SERVICE_EOF'",
          "[Unit]",
          "Description=filings-watcher classify reconciler (heal orphaned filings)",
          "Documentation=https://github.com/PinchasLev/filings-watcher",
          "After=network-online.target",
          "Wants=network-online.target",
          "",
          "[Service]",
          "Type=oneshot",
          "User=filings",
          "Group=filings",
          "TimeoutStartSec=12m",
          "ExecStart=/usr/bin/flock -n --conflict-exit-code=0 /var/lib/filings-watcher/reclassify-orphans.lock /usr/local/bin/filings-reclassify-orphans-tick",
          "StandardOutput=journal",
          "StandardError=journal",
          "SyslogIdentifier=filings-reclassify-orphans",
          "NoNewPrivileges=true",
          "PrivateTmp=true",
          "SERVICE_EOF",
          "cat > /etc/systemd/system/filings-reclassify-orphans.timer <<'TIMER_EOF'",
          "[Unit]",
          "Description=Periodic classify reconciler (orphan recovery, ADR 0030)",
          "Documentation=https://github.com/PinchasLev/filings-watcher",
          "",
          "[Timer]",
          "Unit=filings-reclassify-orphans.service",
          "OnUnitInactiveSec=20min",
          "",
          "[Install]",
          "WantedBy=timers.target",
          "TIMER_EOF",
          "systemctl daemon-reload",
          "systemctl enable filings-daily-index.timer",
          "systemctl enable filings-atom-feed.timer",
          "systemctl enable filings-reclassify-orphans.timer",
          # Fire one Atom invocation now so OnUnitInactiveSec has a
          # reference timestamp; subsequent invocations are scheduled 30
          # seconds after each one exits. systemctl start --no-block
          # returns immediately so this SSM step does not wait for the
          # run. The daily-index timer is OnCalendar and needs no
          # priming.
          "systemctl start --no-block filings-atom-feed.service",
          # Prime the reconciler too so OnUnitInactiveSec has a reference
          # timestamp; --no-block returns immediately, so this primed heal runs
          # in the background and does not hold up the SSM step.
          "systemctl start --no-block filings-reclassify-orphans.service",
          "systemctl start filings-daily-index.timer",
          "systemctl start filings-atom-feed.timer",
          "systemctl start filings-reclassify-orphans.timer",
          "systemctl status --no-pager filings-daily-index.timer || true",
          "systemctl status --no-pager filings-atom-feed.timer || true",
          "systemctl status --no-pager filings-reclassify-orphans.timer || true",
          "echo \"install + enable of filings-daily-index.timer, filings-atom-feed.timer, and filings-reclassify-orphans.timer complete\"",
        ]
      }
    }]
  })

  tags = {
    Name = "filings-install-orchestrate-timer"
  }
}
