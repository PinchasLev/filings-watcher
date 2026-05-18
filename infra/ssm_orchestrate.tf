# Manual-trigger SSM document for running one orchestrator pass. The
# operator invokes this when iterating on the classifier behavior or
# producing the first real classifications before the systemd timer
# (scheduled cadence) lands.
#
# Secrets are fetched from Parameter Store inside the document (see
# ADR 0020); the operator never handles the key material directly.

resource "aws_ssm_document" "orchestrate_once" {
  name            = "filings-orchestrate-once"
  document_type   = "Command"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Run one classify-filing pass against a ticker"
    parameters = {
      ticker = {
        type           = "String"
        description    = "Stock ticker symbol, e.g. AAPL"
        allowedPattern = "^[A-Z]{1,5}(\\.[A-Z])?$"
      }
      filingIndex = {
        type           = "String"
        description    = "Zero-based index of the recent filing to classify (0 = most recent)"
        default        = "0"
        allowedPattern = "^[0-9]{1,3}$"
      }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "orchestrate"
      inputs = {
        runCommand = [
          "set -euo pipefail",
          "TICKER='{{ticker}}'",
          "FILING_INDEX='{{filingIndex}}'",
          "RELEASE_DIR=/opt/filings-watcher/current",
          "if [ ! -d $RELEASE_DIR/orchestrator ]; then",
          "  echo \"orchestrator not present in current release; deploy a SHA whose tarball includes orchestrator/\" >&2",
          "  exit 1",
          "fi",
          "ANTHROPIC_API_KEY=$(aws ssm get-parameter --name /filings-watcher/anthropic-api-key --with-decryption --query Parameter.Value --output text --region ${var.aws_region})",
          "LANGSMITH_API_KEY=$(aws ssm get-parameter --name /filings-watcher/langsmith-api-key --with-decryption --query Parameter.Value --output text --region ${var.aws_region})",
          "EDGAR_USER_AGENT=$(aws ssm get-parameter --name /filings-watcher/edgar-user-agent --with-decryption --query Parameter.Value --output text --region ${var.aws_region})",
          "export ANTHROPIC_API_KEY LANGSMITH_API_KEY EDGAR_USER_AGENT",
          "sudo -u filings -H --preserve-env=ANTHROPIC_API_KEY,LANGSMITH_API_KEY,EDGAR_USER_AGENT bash -c \"cd $RELEASE_DIR/orchestrator && FILINGS_DB_PATH=/var/lib/filings-watcher/filings.db /home/filings/.local/bin/uv run --no-sync classify-filing $TICKER $FILING_INDEX\"",
          "unset ANTHROPIC_API_KEY LANGSMITH_API_KEY EDGAR_USER_AGENT",
          "echo \"orchestrate-once complete for $TICKER index $FILING_INDEX\"",
        ]
      }
    }]
  })

  tags = {
    Name = "filings-orchestrate-once"
  }
}
