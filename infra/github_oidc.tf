# GitHub Actions OIDC trust. Workflow runs present a short-lived OIDC
# token; AWS exchanges it for temporary credentials. No long-lived
# secrets are stored in GitHub.

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

locals {
  github_repo = "PinchasLev/filings-watcher"
}

# --- Build role: trusted by main-branch workflow runs only.
#     Writes release tarballs to s3://filingsradar-artifacts/releases/. ---

data "aws_iam_policy_document" "github_build_assume_role" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${local.github_repo}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_build" {
  name               = "filings-watcher-github-build"
  description        = "GitHub Actions: build job uploads release tarballs to S3."
  assume_role_policy = data.aws_iam_policy_document.github_build_assume_role.json
}

data "aws_iam_policy_document" "github_build" {
  statement {
    actions = [
      "s3:PutObject",
      "s3:AbortMultipartUpload",
    ]
    resources = ["${aws_s3_bucket.artifacts.arn}/releases/*"]
  }
}

resource "aws_iam_role_policy" "github_build" {
  role   = aws_iam_role.github_build.id
  policy = data.aws_iam_policy_document.github_build.json
}

# --- Deploy role: trusted only when the deploy workflow declares the
#     `aws-deploy` GitHub environment. Permissions: invoke one specific
#     SSM document against the production instance. ---

data "aws_iam_policy_document" "github_deploy_assume_role" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${local.github_repo}:environment:aws-deploy"]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name               = "filings-watcher-github-deploy"
  description        = "GitHub Actions: deploy job invokes the filings-deploy SSM document."
  assume_role_policy = data.aws_iam_policy_document.github_deploy_assume_role.json
}

data "aws_iam_policy_document" "github_deploy" {
  statement {
    actions   = ["ssm:SendCommand"]
    resources = [aws_instance.host.arn]
  }
  statement {
    actions = ["ssm:SendCommand"]
    resources = [
      "arn:aws:ssm:${var.aws_region}:*:document/${aws_ssm_document.deploy.name}",
    ]
  }
  statement {
    actions = [
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy.json
}
