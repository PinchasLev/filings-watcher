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
