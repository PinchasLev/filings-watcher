# SSM document that installs the OpenTelemetry Collector (Contrib distribution)
# under systemd, drops a verification-only config (debug + prometheus exporters,
# no backend yet), and starts the service.
#
# Why an SSM document rather than user_data: the operator iterates on Collector
# config and version without forcing instance replacement. Substrate move to
# user_data is a future refactor once the install script is stable.
#
# Why Contrib (not Core): we need receivers (journald, in PR 2) and exporters
# (prometheus, eventually awscloudwatchlogs/metrics) that are not in the Core
# distribution. ADR 0018 deferred the distribution pick to first install; this
# is that pick.
#
# Why no backend exporter in this install: PR 1 scope is foundation +
# verification only. The Collector boots, accepts OTLP on localhost, exposes
# a Prometheus scrape endpoint, and prints arrivals to journald via the debug
# exporter. PR 2 adds the journald receiver + first backend exporter (with
# the IAM that requires). See the "foundation-over-flash" rule: verification
# surfaces are foundation; dashboards are downstream.
#
# Operator verification recipes are documented in infra/README.md.

resource "aws_ssm_document" "install_otel_collector" {
  name            = "filings-install-otel-collector"
  document_type   = "Command"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install + enable the OpenTelemetry Collector (Contrib) with a verification-only multi-pipeline config"
    parameters = {
      version = {
        type        = "String"
        description = "OpenTelemetry Collector Contrib version (e.g., 0.121.0)"
        default     = var.otel_collector_version
      }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "install"
      inputs = {
        runCommand = [
          "set -euo pipefail",
          "VERSION=\"{{ version }}\"",
          "ARCH=\"$(uname -m)\"",
          "case \"$ARCH\" in",
          "  aarch64) RPM_ARCH=\"arm64\" ;;",
          "  x86_64)  RPM_ARCH=\"amd64\" ;;",
          "  *) echo \"unsupported architecture: $ARCH\" >&2; exit 1 ;;",
          "esac",
          "RPM_NAME=\"otelcol-contrib_$${VERSION}_linux_$${RPM_ARCH}.rpm\"",
          "RPM_URL=\"https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v$${VERSION}/$${RPM_NAME}\"",
          "TMP_RPM=\"/tmp/$${RPM_NAME}\"",
          "echo \"installing otelcol-contrib version $${VERSION} for $${RPM_ARCH}\"",
          "curl --fail --silent --show-error --location --output \"$${TMP_RPM}\" \"$${RPM_URL}\"",
          "dnf install -y \"$${TMP_RPM}\"",
          "rm -f \"$${TMP_RPM}\"",
          "install -d -o root -g root -m 0755 /etc/otelcol-contrib",
          "cat > /etc/otelcol-contrib/config.yaml <<'CONFIG_EOF'",
          "# OpenTelemetry Collector config.",
          "#",
          "# Receivers:",
          "#   - otlp on localhost (apps push via OTel SDKs — wired in later PRs).",
          "#   - journald tailing filings-orchestrate.service; the orchestrator",
          "#     emits one JSON line per structured event. The json_parser",
          "#     operator parses MESSAGE and lifts its fields into attributes,",
          "#     where journald envelope fields (_HOSTNAME, _SYSTEMD_UNIT, _PID,",
          "#     etc.) already live. Envelope and app fields are co-queryable in",
          "#     CloudWatch Logs Insights at the same flat level — no JSON",
          "#     decoding in queries.",
          "#",
          "# Exporters:",
          "#   - debug: prints arrivals to stdout (journal).",
          "#   - prometheus: exposes incoming metrics on :8889 for curl verification.",
          "#   - awscloudwatchlogs: ships logs to CloudWatch (log group auto-created",
          "#     on first write; IAM scoped to /filings-watcher/* in iam.tf).",
          "",
          "receivers:",
          "  otlp:",
          "    protocols:",
          "      grpc:",
          "        endpoint: 127.0.0.1:4317",
          "      http:",
          "        endpoint: 127.0.0.1:4318",
          "  journald:",
          "    units:",
          "      - filings-orchestrate.service",
          "    priority: info",
          "    operators:",
          "      - type: json_parser",
          "        parse_from: body",
          "        parse_to: attributes",
          "        on_error: send_quiet",
          "",
          "processors:",
          "  batch:",
          "    timeout: 10s",
          "    send_batch_size: 1024",
          "",
          "exporters:",
          "  debug:",
          "    verbosity: detailed",
          "  prometheus:",
          "    endpoint: 127.0.0.1:8889",
          "    namespace: filings_watcher",
          "    send_timestamps: true",
          "  awscloudwatchlogs:",
          "    log_group_name: /filings-watcher/orchestrator",
          "    log_stream_name: filings-watcher-host",
          "    region: \"${var.aws_region}\"",
          "",
          "service:",
          "  pipelines:",
          "    metrics:",
          "      receivers:  [otlp]",
          "      processors: [batch]",
          "      exporters:  [debug, prometheus]",
          "    traces:",
          "      receivers:  [otlp]",
          "      processors: [batch]",
          "      exporters:  [debug]",
          "    logs:",
          "      receivers:  [otlp, journald]",
          "      processors: [batch]",
          "      exporters:  [debug, awscloudwatchlogs]",
          "  telemetry:",
          "    logs:",
          "      level: info",
          "CONFIG_EOF",
          "chown root:root /etc/otelcol-contrib/config.yaml",
          "chmod 0644 /etc/otelcol-contrib/config.yaml",
          "systemctl daemon-reload",
          "systemctl enable otelcol-contrib.service",
          "systemctl restart otelcol-contrib.service",
          "sleep 2",
          "systemctl status --no-pager otelcol-contrib.service || true",
          "echo \"install + enable of otelcol-contrib complete\"",
        ]
      }
    }]
  })

  tags = {
    Name = "filings-install-otel-collector"
  }
}
