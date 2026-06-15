data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "host" {
  name               = "filings-watcher-host-role"
  description        = "Role attached to the v0 EC2 host. SSM Session Manager access plus read on the artifact bucket for pulling release tarballs."
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
}

resource "aws_iam_role_policy_attachment" "ssm_managed" {
  role       = aws_iam_role.host.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "host_artifacts_read" {
  statement {
    actions = [
      "s3:GetObject",
    ]
    resources = ["${aws_s3_bucket.artifacts.arn}/releases/*"]
  }
}

resource "aws_iam_role_policy" "host_artifacts_read" {
  name   = "filings-watcher-host-artifacts-read"
  role   = aws_iam_role.host.id
  policy = data.aws_iam_policy_document.host_artifacts_read.json
}

# SSM Parameter Store read for third-party API credentials (Anthropic,
# LangSmith). Operator places values out-of-band per ADR 0020.
data "aws_iam_policy_document" "host_secrets_read" {
  statement {
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
    ]
    resources = [
      "arn:aws:ssm:${var.aws_region}:*:parameter/filings-watcher/*",
    ]
  }
}

resource "aws_iam_role_policy" "host_secrets_read" {
  name   = "filings-watcher-host-secrets-read"
  role   = aws_iam_role.host.id
  policy = data.aws_iam_policy_document.host_secrets_read.json
}

resource "aws_iam_instance_profile" "host" {
  name = "filings-watcher-host-profile"
  role = aws_iam_role.host.name
}

# OpenTelemetry Collector ships logs (and, in a later PR, metrics) to
# CloudWatch. Scoped to log groups under /filings-watcher/ so the role
# cannot read or write into unrelated CloudWatch streams. Log groups are
# auto-created by the awscloudwatchlogs exporter on first write — no
# explicit group resource here for v0.
data "aws_iam_policy_document" "host_otel_cloudwatch_logs" {
  statement {
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = [
      "arn:aws:logs:${var.aws_region}:*:log-group:/filings-watcher/*",
      "arn:aws:logs:${var.aws_region}:*:log-group:/filings-watcher/*:log-stream:*",
    ]
  }
}

resource "aws_iam_role_policy" "host_otel_cloudwatch_logs" {
  name   = "filings-watcher-host-otel-cloudwatch-logs"
  role   = aws_iam_role.host.id
  policy = data.aws_iam_policy_document.host_otel_cloudwatch_logs.json
}

# The host pushes liveness heartbeats (HostHeartbeat, DrainerHeartbeat) to
# CloudWatch for the dead-man's-switch (ADR 0031). PutMetricData has no
# resource-level permissions, so it is scoped instead by a condition on the
# metric namespace — the role can write only to the filings-watcher namespace,
# not to arbitrary CloudWatch metrics.
data "aws_iam_policy_document" "host_cloudwatch_metrics" {
  statement {
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["filings-watcher"]
    }
  }
}

resource "aws_iam_role_policy" "host_cloudwatch_metrics" {
  name   = "filings-watcher-host-cloudwatch-metrics"
  role   = aws_iam_role.host.id
  policy = data.aws_iam_policy_document.host_cloudwatch_metrics.json
}
