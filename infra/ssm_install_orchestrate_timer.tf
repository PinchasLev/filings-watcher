# SSM document that installs the periodic ingest under systemd: five
# wrapper scripts, five oneshot service units, five timers, and a shared
# classifier resource slice (ADR 0035) — the
# Atom feed (ADR 0029, near-real-time), the daily-index reconciliation
# backstop (ADR 0021, evening cluster per ADR 0029), the classify
# reconciler that heals orphaned filings (ADR 0030), the alarm drainer
# that delivers queued alerts to Discord (ADR 0031), and the host
# heartbeat that feeds the external CloudWatch dead-man's-switch (ADR
# 0031). The operator runs this once per host (e.g., after the first
# deploy on a new instance).
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
#   - Alarm drainer: OnUnitInactiveSec=2m. Reads alerts_outbox and POSTs
#     undelivered rows to the right Discord channel by severity (ADR 0031),
#     so a panic or dead-letter reaches the operator within ~2 minutes. Pure
#     delivery — no Anthropic/EDGAR credential, no cost cap (it never
#     classifies or fetches). Its TimeoutStartSec is 5m, not 12m: a drain
#     pass is a handful of HTTP POSTs, so a pass still running after 5 min is
#     stuck and should be reaped. Idempotent across runs (delivered_at marks
#     a row done) and flock-guarded like the others.
#   - The three ingest/reconciler ticks: TimeoutStartSec=12m bounds a stuck
#     tick (SIGTERM at 12 min) and flock -n --conflict-exit-code=0 protects
#     against an operator-triggered run racing a scheduled one.
#
# An initial invocation of the Atom service is fired explicitly at the
# end of the install so OnUnitInactiveSec has a reference point —
# without that, the timer would wait forever for the service to have
# been "inactive" at least once. The daily-index timer uses OnCalendar
# and needs no priming; it will fire at the next 22:15 ET window.
#
# Self-arming across reboots (ADR 0035): the OnUnitInactiveSec timers
# (atom-feed, reclassify-orphans, alarm-drain, host-heartbeat) also carry
# OnBootSec=, so they fire once shortly after every boot and re-acquire
# their inactive reference. Without it, a plain reboot/stop-start leaves
# them loaded-but-unscheduled until the install doc is re-run — which on
# 2026-06-22 silently halted both ingestion AND the heartbeat (so the
# dead-man's-switch could not even clear) after an instance stop/start.
#
# Resource isolation (ADR 0035): the three Anthropic-classifying ticks
# run under a shared filings-classify.slice with MemoryMax + MemorySwapMax=0,
# so a runaway tick is OOMKilled inside its own cgroup instead of swap-
# thrashing the whole host into an unresponsive wedge (the 2026-06-22
# incident: memory pressure with swap present => livelock, not a clean crash).
#
# Secrets are fetched from SSM Parameter Store inside each wrapper at
# run time, matching the operator-seeded pattern from ADR 0020.

resource "aws_ssm_document" "install_orchestrate_timer" {
  name            = "filings-install-orchestrate-timer"
  document_type   = "Command"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install + enable the systemd timers for scan-atom-feed (30s), scan-daily-index (evening cluster), reclassify-orphans (20m reconciler), alarm-drain (2m alert delivery), and host-heartbeat (5m dead-man's-switch)"
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
          # --- Alarm-drain wrapper ---
          # One delivery pass: read alerts_outbox and POST undelivered rows to
          # the right Discord channel by severity (ADR 0031). Fetches the two
          # channel webhook URLs from Parameter Store at run time (operator-
          # seeded SecureStrings, never in Terraform state). Needs no Anthropic
          # or EDGAR credential and no cost cap — it never classifies or fetches,
          # it only reads a table and POSTs. ALERT_INFO_TTL_MINUTES and
          # ALERT_REPEAT_HOURS are left unset so the drainer uses its config.py
          # defaults (30m / 4h); set them here to override.
          "cat > /usr/local/bin/filings-alarm-drain-tick <<'TICK_EOF'",
          "#!/bin/bash",
          "set -euo pipefail",
          "DISCORD_ALERTS_WEBHOOK_URL=$(aws ssm get-parameter --name /filings-watcher/discord-alerts-webhook-url --with-decryption --query Parameter.Value --output text --region ${var.aws_region})",
          "DISCORD_INFO_WEBHOOK_URL=$(aws ssm get-parameter --name /filings-watcher/discord-info-webhook-url --with-decryption --query Parameter.Value --output text --region ${var.aws_region})",
          "export DISCORD_ALERTS_WEBHOOK_URL DISCORD_INFO_WEBHOOK_URL",
          "export FILINGS_DB_PATH=/var/lib/filings-watcher/filings.db",
          "cd /opt/filings-watcher/current/orchestrator",
          "set +e",
          "/home/filings/.local/bin/uv run --no-sync alarm-drain",
          "rc=$?",
          "set -e",
          # DrainerHeartbeat for the dead-man's-switch (ADR 0031): emit when the
          # pass actually ran — rc 0 (clean) or rc 1 (some POSTs failed but the
          # process is alive and retries next pass). Withhold on rc >= 2
          # (misconfig / hard failure) so the drainer-heartbeat-missing alarm
          # fires. This proves the alert-delivery path itself is alive, which the
          # generic HostHeartbeat cannot. A failed metric push does not fail the
          # unit (|| true) — a transient miss won't trip the 15-min alarm, and a
          # persistent one IS the signal.
          "if [ \"$rc\" -eq 0 ] || [ \"$rc\" -eq 1 ]; then",
          "  aws cloudwatch put-metric-data --namespace filings-watcher --metric-name DrainerHeartbeat --value 1 --region ${var.aws_region} || true",
          "fi",
          "exit $rc",
          "TICK_EOF",
          "chmod 0755 /usr/local/bin/filings-alarm-drain-tick",
          "chown root:root /usr/local/bin/filings-alarm-drain-tick",
          # --- Host-heartbeat wrapper ---
          # Pushes a HostHeartbeat metric to CloudWatch for the dead-man's-switch
          # (ADR 0031). A pure box-liveness probe — touches no app state, runs as
          # root, deliberately independent of the filings user / DB / release
          # tree, so it keeps signalling even if the app is wedged. If the box is
          # dead, unreachable, or its IAM/network to CloudWatch is broken, the
          # metric stops arriving and the heartbeat-missing alarm fires.
          "cat > /usr/local/bin/filings-host-heartbeat-tick <<'TICK_EOF'",
          "#!/bin/bash",
          "set -euo pipefail",
          "exec aws cloudwatch put-metric-data --namespace filings-watcher --metric-name HostHeartbeat --value 1 --region ${var.aws_region}",
          "TICK_EOF",
          "chmod 0755 /usr/local/bin/filings-host-heartbeat-tick",
          "chown root:root /usr/local/bin/filings-host-heartbeat-tick",
          "install -d -o filings -g filings -m 0755 /var/lib/filings-watcher",
          # --- Classifier resource slice (memory isolation, ADR 0035) ---
          # All Anthropic-classifying ticks (daily-index, atom-feed, reclassify-
          # orphans) run under this shared slice, so their TOTAL memory is bounded
          # regardless of how many run at once. MemoryMax is a safety CEILING, not
          # a target (a legitimate tick measures ~150-400MB); MemorySwapMax=0 makes
          # a runaway OOMKill inside this cgroup rather than swap-thrash the host
          # into a wedge. 2G leaves ~1.7GB host reserve (OS + Caddy + Go server +
          # page cache + margin) on t4g.medium. Tune from `systemctl show -p
          # MemoryPeak` once observed under load. The lightweight ticks (alarm-drain,
          # host-heartbeat) are deliberately outside the slice — they never classify.
          "cat > /etc/systemd/system/filings-classify.slice <<'SLICE_EOF'",
          "[Unit]",
          "Description=filings-watcher classifier resource slice (memory isolation, ADR 0035)",
          "",
          "[Slice]",
          "MemoryMax=2G",
          "MemorySwapMax=0",
          "SLICE_EOF",
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
          "Slice=filings-classify.slice",
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
          "Slice=filings-classify.slice",
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
          "OnBootSec=1min",
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
          "Slice=filings-classify.slice",
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
          "OnBootSec=3min",
          "",
          "[Install]",
          "WantedBy=timers.target",
          "TIMER_EOF",
          # --- Alarm-drain service + timer ---
          # OnUnitInactiveSec=2min: schedule the next drain 2 minutes after the
          # previous one exits, so passes never overlap and an undelivered row
          # waits at most ~2 min plus the previous pass. Its own lock file lets
          # it run independently of the ingest/reconciler ticks. TimeoutStartSec
          # is 5m (a drain is just HTTP POSTs); a longer-running pass is stuck.
          "cat > /etc/systemd/system/filings-alarm-drain.service <<'SERVICE_EOF'",
          "[Unit]",
          "Description=filings-watcher alarm drainer (deliver queued alerts to Discord)",
          "Documentation=https://github.com/PinchasLev/filings-watcher",
          "After=network-online.target",
          "Wants=network-online.target",
          "",
          "[Service]",
          "Type=oneshot",
          "User=filings",
          "Group=filings",
          "TimeoutStartSec=5m",
          "ExecStart=/usr/bin/flock -n --conflict-exit-code=0 /var/lib/filings-watcher/alarm-drain.lock /usr/local/bin/filings-alarm-drain-tick",
          "StandardOutput=journal",
          "StandardError=journal",
          "SyslogIdentifier=filings-alarm-drain",
          "NoNewPrivileges=true",
          "PrivateTmp=true",
          "SERVICE_EOF",
          "cat > /etc/systemd/system/filings-alarm-drain.timer <<'TIMER_EOF'",
          "[Unit]",
          "Description=Periodic alarm drainer (deliver queued alerts, ADR 0031)",
          "Documentation=https://github.com/PinchasLev/filings-watcher",
          "",
          "[Timer]",
          "Unit=filings-alarm-drain.service",
          "OnUnitInactiveSec=2min",
          "OnBootSec=90s",
          "",
          "[Install]",
          "WantedBy=timers.target",
          "TIMER_EOF",
          # --- Host-heartbeat service + timer ---
          # OnUnitInactiveSec=5min. Runs as root (no User= line) — a box-liveness
          # probe independent of the app user. No flock: a single idempotent
          # metric push, no shared state to guard.
          "cat > /etc/systemd/system/filings-host-heartbeat.service <<'SERVICE_EOF'",
          "[Unit]",
          "Description=filings-watcher host heartbeat (CloudWatch dead-man's-switch)",
          "Documentation=https://github.com/PinchasLev/filings-watcher",
          "After=network-online.target",
          "Wants=network-online.target",
          "",
          "[Service]",
          "Type=oneshot",
          "TimeoutStartSec=2m",
          "ExecStart=/usr/local/bin/filings-host-heartbeat-tick",
          "StandardOutput=journal",
          "StandardError=journal",
          "SyslogIdentifier=filings-host-heartbeat",
          "NoNewPrivileges=true",
          "PrivateTmp=true",
          "SERVICE_EOF",
          "cat > /etc/systemd/system/filings-host-heartbeat.timer <<'TIMER_EOF'",
          "[Unit]",
          "Description=Periodic host heartbeat (CloudWatch dead-man's-switch, ADR 0031)",
          "Documentation=https://github.com/PinchasLev/filings-watcher",
          "",
          "[Timer]",
          "Unit=filings-host-heartbeat.service",
          "OnUnitInactiveSec=5min",
          "OnBootSec=1min",
          "",
          "[Install]",
          "WantedBy=timers.target",
          "TIMER_EOF",
          "systemctl daemon-reload",
          "systemctl enable filings-daily-index.timer",
          "systemctl enable filings-atom-feed.timer",
          "systemctl enable filings-reclassify-orphans.timer",
          "systemctl enable filings-alarm-drain.timer",
          "systemctl enable filings-host-heartbeat.timer",
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
          # Prime the drainer too so OnUnitInactiveSec has a reference timestamp;
          # --no-block returns immediately. This first pass delivers whatever has
          # queued in alerts_outbox since the producers shipped, subject to the
          # freshness window (stale INFO is retired without posting).
          "systemctl start --no-block filings-alarm-drain.service",
          # Prime the host heartbeat so OnUnitInactiveSec has a reference and the
          # first metric lands immediately (otherwise the alarm, which treats
          # missing data as breaching, would page before the first push).
          "systemctl start --no-block filings-host-heartbeat.service",
          "systemctl start filings-daily-index.timer",
          "systemctl start filings-atom-feed.timer",
          "systemctl start filings-reclassify-orphans.timer",
          "systemctl start filings-alarm-drain.timer",
          "systemctl start filings-host-heartbeat.timer",
          "systemctl status --no-pager filings-daily-index.timer || true",
          "systemctl status --no-pager filings-atom-feed.timer || true",
          "systemctl status --no-pager filings-reclassify-orphans.timer || true",
          "systemctl status --no-pager filings-alarm-drain.timer || true",
          "systemctl status --no-pager filings-host-heartbeat.timer || true",
          "echo \"install + enable of filings-daily-index.timer, filings-atom-feed.timer, filings-reclassify-orphans.timer, filings-alarm-drain.timer, and filings-host-heartbeat.timer complete\"",
        ]
      }
    }]
  })

  tags = {
    Name = "filings-install-orchestrate-timer"
  }
}
