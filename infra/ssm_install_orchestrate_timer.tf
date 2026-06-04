# SSM document that installs the periodic daily-index ingest under systemd:
# a wrapper script, a oneshot service unit, and a timer that fires every
# 15 minutes after the previous invocation exited. The operator runs this
# once per host (e.g., after the first deploy on a new instance).
#
# Why an SSM document rather than user_data: the timer depends on the
# orchestrator release tree being present on disk (`/opt/filings-watcher/current/orchestrator`),
# which only exists after the first `filings-deploy` SSM run. Installing
# at first-boot would race that ordering. Installing on demand keeps the
# install ordered after the deploy.
#
# Per ADR 0012:
#   - OnUnitInactiveSec=15min — schedule next run after previous exited.
#   - TimeoutStartSec=12m — bound the invocation; SIGTERM at 12 min.
#   - flock -n with --conflict-exit-code=0 — overlap-safe; if a previous
#     run is still holding the lock, the new run exits cleanly without
#     emitting a tick_failed.
#
# An initial invocation is fired explicitly at the end of the install
# (systemctl start filings-orchestrate.service) so OnUnitInactiveSec has
# a reference point — without that, the timer would wait forever for
# the service to have been "inactive" at least once.
#
# Secrets are fetched from SSM Parameter Store inside the wrapper at run
# time, matching the operator-seeded pattern from ADR 0020.

resource "aws_ssm_document" "install_orchestrate_timer" {
  name            = "filings-install-orchestrate-timer"
  document_type   = "Command"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install + enable the systemd timer that runs scan-daily-index every 15 minutes"
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
          "cat > /usr/local/bin/filings-orchestrate-tick <<'TICK_EOF'",
          "#!/bin/bash",
          "# Single daily-index ingest invocation. Fetches secrets from",
          "# Parameter Store and execs scan-daily-index. Invoked by",
          "# filings-orchestrate.service (which wraps this in flock -n).",
          "set -euo pipefail",
          "ANTHROPIC_API_KEY=$(aws ssm get-parameter --name /filings-watcher/anthropic-api-key --with-decryption --query Parameter.Value --output text --region ${var.aws_region})",
          "LANGSMITH_API_KEY=$(aws ssm get-parameter --name /filings-watcher/langsmith-api-key --with-decryption --query Parameter.Value --output text --region ${var.aws_region})",
          "EDGAR_USER_AGENT=$(aws ssm get-parameter --name /filings-watcher/edgar-user-agent --with-decryption --query Parameter.Value --output text --region ${var.aws_region})",
          "export ANTHROPIC_API_KEY LANGSMITH_API_KEY EDGAR_USER_AGENT",
          "export FILINGS_DB_PATH=/var/lib/filings-watcher/filings.db",
          # ADR 0029 spend-cap surface. Operator-tunable starting values; update
          # in source and re-apply terraform to push to the host. The orchestrator
          # falls back to the same defaults via config.py if these are unset, so
          # an outdated wrapper does not silently disable the cap.
          "export ANTHROPIC_DAILY_COST_CAP_USD=5.00",
          "export ANTHROPIC_DAILY_COST_WARN_USD=4.00",
          # OpenTelemetry configuration. The orchestrator's setup_otel()
          # reads these standard env vars; if OTEL_EXPORTER_OTLP_ENDPOINT
          # is unset (e.g., during local dev) the SDK stays no-op. The
          # service.version resource attribute is derived from the
          # release directory the symlink resolves to.
          "RELEASE_SHA=$(basename $(readlink -f /opt/filings-watcher/current))",
          "export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317",
          "export OTEL_EXPORTER_OTLP_PROTOCOL=grpc",
          "export OTEL_SERVICE_NAME=filings-orchestrator",
          "export OTEL_RESOURCE_ATTRIBUTES=service.namespace=filings-watcher,service.version=$RELEASE_SHA",
          # Opt into the latest gen_ai.* semantic conventions. Stable as
          # of mid-2026 but still gated behind this opt-in flag.
          "export OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental",
          # Disable Traceloop's prompt/completion content attributes on
          # spans. 8-K bodies (up to 12k chars per Item, classified
          # individually) would inflate trace volume without adding
          # signal we don't already have in LangSmith. Tokens, model
          # name, latency, and cache hit/miss still ride along.
          "export TRACELOOP_TRACE_CONTENT=false",
          "cd /opt/filings-watcher/current/orchestrator",
          "exec /home/filings/.local/bin/uv run --no-sync scan-daily-index",
          "TICK_EOF",
          "chmod 0755 /usr/local/bin/filings-orchestrate-tick",
          "chown root:root /usr/local/bin/filings-orchestrate-tick",
          "install -d -o filings -g filings -m 0755 /var/lib/filings-watcher",
          "cat > /etc/systemd/system/filings-orchestrate.service <<'SERVICE_EOF'",
          "[Unit]",
          "Description=filings-watcher daily-index ingest (one invocation)",
          "Documentation=https://github.com/PinchasLev/filings-watcher",
          "After=network-online.target",
          "Wants=network-online.target",
          "",
          "[Service]",
          "Type=oneshot",
          "User=filings",
          "Group=filings",
          "TimeoutStartSec=12m",
          "ExecStart=/usr/bin/flock -n --conflict-exit-code=0 /var/lib/filings-watcher/orchestrate.lock /usr/local/bin/filings-orchestrate-tick",
          "StandardOutput=journal",
          "StandardError=journal",
          "SyslogIdentifier=filings-orchestrate",
          "NoNewPrivileges=true",
          "PrivateTmp=true",
          "SERVICE_EOF",
          "cat > /etc/systemd/system/filings-orchestrate.timer <<'TIMER_EOF'",
          "[Unit]",
          "Description=Periodic invocation of the daily-index ingest",
          "Documentation=https://github.com/PinchasLev/filings-watcher",
          "",
          "[Timer]",
          "Unit=filings-orchestrate.service",
          "OnUnitInactiveSec=15min",
          "",
          "[Install]",
          "WantedBy=timers.target",
          "TIMER_EOF",
          "systemctl daemon-reload",
          "systemctl enable filings-orchestrate.timer",
          # Fire one invocation now so OnUnitInactiveSec has a reference
          # timestamp; subsequent invocations are scheduled 15 minutes
          # after each one exits. systemctl start --no-block returns
          # immediately so this SSM step does not wait for the run.
          "systemctl start --no-block filings-orchestrate.service",
          "systemctl start filings-orchestrate.timer",
          "systemctl status --no-pager filings-orchestrate.timer || true",
          "echo \"install + enable of filings-orchestrate.timer complete\"",
        ]
      }
    }]
  })

  tags = {
    Name = "filings-install-orchestrate-timer"
  }
}
