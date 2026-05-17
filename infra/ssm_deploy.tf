# SSM document encapsulating the host-side deploy procedure. The
# GitHub Actions deploy workflow calls ssm:SendCommand with this
# document name and the target release SHA; the SSM agent on the host
# runs the steps below.
#
# Keeping the deploy logic here (rather than in user_data or in the
# release tarball) means deploy-script iteration is `terraform apply`,
# not an EC2 instance replacement or a release-only-for-script-change.

resource "aws_ssm_document" "deploy" {
  name            = "filings-deploy"
  document_type   = "Command"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Deploy a filings-watcher release by SHA"
    parameters = {
      sha = {
        type           = "String"
        description    = "Git commit SHA of the release to deploy. Must exist at s3://filingsradar-artifacts/releases/<sha>/release.tar.gz"
        allowedPattern = "^[0-9a-f]{7,40}$"
      }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "deploy"
      inputs = {
        runCommand = [
          "set -euo pipefail",
          "SHA='{{sha}}'",
          "RELEASES_DIR=/opt/filings-watcher/releases",
          "TARBALL=/tmp/release-$SHA.tar.gz",
          "aws s3 cp s3://${aws_s3_bucket.artifacts.bucket}/releases/$SHA/release.tar.gz $TARBALL",
          "mkdir -p $RELEASES_DIR/$SHA",
          "tar -xzf $TARBALL -C $RELEASES_DIR/$SHA",
          "chown -R filings:filings $RELEASES_DIR/$SHA",
          "rm -f $TARBALL",
          "ln -sfn $RELEASES_DIR/$SHA /opt/filings-watcher/current",
          "systemctl restart filings-server",
          "sleep 2",
          "systemctl is-active filings-server",
          "echo \"deploy of $SHA complete\"",
        ]
      }
    }]
  })

  tags = {
    Name = "filings-deploy"
  }
}
