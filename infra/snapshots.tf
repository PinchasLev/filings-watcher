# AWS Data Lifecycle Manager policy for the data volume.
# Daily snapshot at 06:00 UTC, retain 7 most recent snapshots.

data "aws_iam_policy_document" "dlm_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["dlm.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "dlm_lifecycle" {
  name               = "filings-watcher-dlm-lifecycle"
  description        = "AWS Data Lifecycle Manager role for filings-watcher data volume snapshots."
  assume_role_policy = data.aws_iam_policy_document.dlm_assume_role.json
}

resource "aws_iam_role_policy_attachment" "dlm_lifecycle" {
  role       = aws_iam_role.dlm_lifecycle.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

resource "aws_dlm_lifecycle_policy" "data_daily" {
  description        = "Daily snapshot of filings-watcher-data with 7-day retention"
  execution_role_arn = aws_iam_role.dlm_lifecycle.arn
  state              = "ENABLED"

  policy_details {
    resource_types = ["VOLUME"]

    target_tags = {
      Snapshot = "daily"
    }

    schedule {
      name = "daily-0600-utc"

      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["06:00"]
      }

      retain_rule {
        count = 7
      }

      copy_tags = true

      tags_to_add = {
        SnapshotPolicy = "filings-watcher-daily"
      }
    }
  }
}
