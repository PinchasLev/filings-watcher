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

resource "aws_iam_instance_profile" "host" {
  name = "filings-watcher-host-profile"
  role = aws_iam_role.host.name
}
