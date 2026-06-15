# CloudWatch alarms — the external dead-man's-switch (ADR 0031).
#
# Nothing running ON the host can report that the host is dead; only an
# external observer can. These alarms are that observer. Two are absence
# detectors: the host and the drainer each push a heartbeat metric on a timer
# (see ssm_install_orchestrate_timer.tf), and CloudWatch fires when the metric
# STOPS arriving (treat_missing_data = "breaching"). The third watches EC2's own
# instance/system reachability checks. All publish to the SNS topic -> email.
#
# Two heartbeats, not one, so the alarms are diagnostic: HostHeartbeat proves
# the box + systemd + IAM/network to CloudWatch all work; DrainerHeartbeat
# proves the alert-delivery path specifically is alive. Box dead -> both go
# silent; alarm-drain timer stopped while the box runs -> only DrainerHeartbeat
# goes silent. Either way you are paged; together they say which broke.

# HostHeartbeat stops arriving: box off/terminated, network gone, systemd dead,
# or IAM to CloudWatch broken. Emitted every ~5 min; 3 missing 5-min periods
# (~15 min) before firing, so a single hiccup does not page.
resource "aws_cloudwatch_metric_alarm" "host_heartbeat_missing" {
  alarm_name        = "filings-watcher-host-heartbeat-missing"
  alarm_description = "No HostHeartbeat for ~15 min: the host is dead/unreachable, systemd is down, or its IAM/network to CloudWatch is broken. Nothing on the box can tell you this — that is the point."

  namespace   = "filings-watcher"
  metric_name = "HostHeartbeat"
  statistic   = "Sum"
  period      = 300

  comparison_operator = "LessThanThreshold"
  threshold           = 1
  evaluation_periods  = 3
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]

  tags = {
    Name = "filings-watcher-host-heartbeat-missing"
  }
}

# DrainerHeartbeat stops arriving: the alarm-drain timer was disabled, systemd
# gave up restarting it, or the drainer fails hard every pass. Alerts would then
# pile up undelivered in alerts_outbox and you would never know — this is the
# guard on the alert path itself. Emitted on each (~2 min) drain pass.
resource "aws_cloudwatch_metric_alarm" "drainer_heartbeat_missing" {
  alarm_name        = "filings-watcher-drainer-heartbeat-missing"
  alarm_description = "No DrainerHeartbeat for ~15 min: alarm-drain is not running, so alerts are accumulating undelivered in alerts_outbox. The alerting path has gone silent."

  namespace   = "filings-watcher"
  metric_name = "DrainerHeartbeat"
  statistic   = "Sum"
  period      = 300

  comparison_operator = "LessThanThreshold"
  threshold           = 1
  evaluation_periods  = 3
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]

  tags = {
    Name = "filings-watcher-drainer-heartbeat-missing"
  }
}

# EC2's own reachability checks fail: hypervisor/hardware fault or a wedged
# network stack. Faster (1-min granularity) and AWS-native, complementing the
# heartbeats. Missing data is treated as not-breaching here: a stopped instance
# reports no status checks, and the HostHeartbeat alarm already covers "stopped".
resource "aws_cloudwatch_metric_alarm" "host_status_check_failed" {
  alarm_name        = "filings-watcher-host-status-check-failed"
  alarm_description = "EC2 StatusCheckFailed on the host: instance/system reachability is impaired (hypervisor, hardware, or network stack)."

  namespace   = "AWS/EC2"
  metric_name = "StatusCheckFailed"
  dimensions  = { InstanceId = aws_instance.host.id }
  statistic   = "Maximum"
  period      = 60

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  evaluation_periods  = 3
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]

  tags = {
    Name = "filings-watcher-host-status-check-failed"
  }
}
