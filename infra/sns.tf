# SNS topic for operator alarms — the external dead-man's-switch (ADR 0031).
#
# CloudWatch alarms (see cloudwatch.tf) publish here when the host stops
# signalling liveness or fails an EC2 status check. The topic delivers to the
# operator's email: a channel deliberately INDEPENDENT of the host and of the
# Discord drainer, so the one failure class it exists to catch — the host (and
# thus Discord delivery) being dead — cannot also silence the notification.
#
# The topic is the fan-out seam. Adding a Discord bridge (SNS -> Lambda ->
# webhook, or an EventBridge API destination) or SMS later is just another
# subscription on this same topic — no change to the alarms or the heartbeat
# plumbing. Email is the v0 channel for fewest moving parts and medium
# diversity; richer/louder delivery is a documented fast-follow.

resource "aws_sns_topic" "alarms" {
  name = "filings-watcher-alarms"

  tags = {
    Name = "filings-watcher-alarms"
  }
}

# Email subscription. Terraform creates this in "pending confirmation" state;
# AWS emails a confirmation link that the operator MUST click before any alarm
# notification will deliver. The endpoint comes from gitignored tfvars.
resource "aws_sns_topic_subscription" "alarms_email" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}
