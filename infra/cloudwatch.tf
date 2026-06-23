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

# Memory-pressure alarms (ADR 0035 follow-up). During the 2026-06-22 wedge we were
# blind to memory and swap: default EC2 metrics are hypervisor-level and never
# include guest memory/swap, and no CloudWatch agent is installed. The suspected
# mechanism was memory pressure spilling into swap thrash, but it was never
# confirmed (no OOM-killer in the kernel log; a later drain held flat at ~375 MB) —
# so these metrics exist as much to CONFIRM OR REFUTE that next time as to alarm on
# it. The host-heartbeat tick now emits SwapUsedPercent + MemAvailablePercent every
# ~5 min (see ssm_install_orchestrate_timer.tf). treat_missing_data = notBreaching
# on both: absence of the metric means the box/agent is down, which the
# heartbeat-missing alarm owns.

# Sustained swap use is the best leading indicator we have for host memory
# pressure. The classifier slice runs with MemorySwapMax=0, so it cannot swap — any
# sustained swap is HOST-level pressure (OS, Caddy, the Go server), the suspected
# (unconfirmed) class of condition behind the 2026-06-22 wedge. 20% of the 2 GB
# swap (~400 MB) held for ~10 min is well clear of the ~1% idle baseline.
resource "aws_cloudwatch_metric_alarm" "host_swap_high" {
  alarm_name        = "filings-watcher-host-swap-high"
  alarm_description = "SwapUsedPercent >= 20% for ~10 min: the host is under memory pressure and spilling to swap — the suspected (but unconfirmed) class of condition behind the 2026-06-22 wedge. Investigate (a heavy filing, a leak, or load) before the box degrades."

  namespace   = "filings-watcher"
  metric_name = "SwapUsedPercent"
  statistic   = "Average"
  period      = 300

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 20
  evaluation_periods  = 2
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]

  tags = {
    Name = "filings-watcher-host-swap-high"
  }
}

# Complementary headroom gauge: available memory falling low means the box is
# close to pressure even if swap has not moved yet.
resource "aws_cloudwatch_metric_alarm" "host_memory_low" {
  alarm_name        = "filings-watcher-host-memory-low"
  alarm_description = "MemAvailablePercent <= 10% for ~10 min: very little free memory — the box is approaching the kind of memory pressure that can spill into swap and degrade it."

  namespace   = "filings-watcher"
  metric_name = "MemAvailablePercent"
  statistic   = "Average"
  period      = 300

  comparison_operator = "LessThanOrEqualToThreshold"
  threshold           = 10
  evaluation_periods  = 2
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]

  tags = {
    Name = "filings-watcher-host-memory-low"
  }
}

# A classifier tick was OOM-killed at its 2 GB slice cap (ADR 0035). MemorySwapMax=0
# means a tick exceeding 2 GB is SIGKILLed inside its cgroup — contained (the host
# never wedges) and the timer re-fires — but the SIGKILL bypasses the orchestrator's
# own alerting (ADR 0031), so without this it would be journald-only. The classifier
# services' OnFailure= handler emits ClassifierOOMKill=1 per kill (see
# ssm_install_orchestrate_timer.tf); the metric's sum over time is the kill frequency.
# Kills should be ~never, so any kill pages. treat_missing_data = notBreaching: no
# kills -> no data -> OK.
resource "aws_cloudwatch_metric_alarm" "classifier_oom_killed" {
  alarm_name        = "filings-watcher-classifier-oom-killed"
  alarm_description = "A classifier tick was OOM-killed at the 2 GB slice cap. Contained (the host is fine and the timer re-fires), but a recurring kill means a filing exceeds the cap and will keep being retried without dead-lettering — investigate the filing and its size."

  namespace   = "filings-watcher"
  metric_name = "ClassifierOOMKill"
  statistic   = "Sum"
  period      = 300

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]

  tags = {
    Name = "filings-watcher-classifier-oom-killed"
  }
}

# The public site is failing its external health check (ADR 0036). Route 53 probes
# https://filingsradar.com/health from outside AWS (see route53.tf) and publishes
# HealthCheckStatus — 1 healthy / 0 unhealthy, already debounced by the check's
# failure_threshold. This pages when the SITE is down even if the box looks fine: a
# web-service crash-loop, panic, OOM, expired cert, or DNS failure — the gap the
# host-heartbeat and status-check alarms cannot see. Route 53 health-check metrics
# live in us-east-1 (our region), so this alarm reads them directly.
#
# treat_missing_data = notBreaching here (unlike the heartbeat alarms): the real
# signal is a PRESENT HealthCheckStatus=0 from an AWS-managed continuous metric, not
# an absent one — so breaching would only add a transient false page at create time
# for no real benefit. (The heartbeats use breaching because there the ABSENCE of a
# box-emitted metric is itself the signal.)
resource "aws_cloudwatch_metric_alarm" "site_unhealthy" {
  alarm_name        = "filings-watcher-site-unhealthy"
  alarm_description = "filingsradar.com/health is failing Route 53's external checkers: the public web service is down (crash-loop, panic, OOM, expired cert, or DNS/host failure) — even if the host itself looks healthy."

  namespace   = "AWS/Route53"
  metric_name = "HealthCheckStatus"
  dimensions  = { HealthCheckId = aws_route53_health_check.site.id }
  statistic   = "Minimum"
  period      = 60

  comparison_operator = "LessThanThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]

  tags = {
    Name = "filings-watcher-site-unhealthy"
  }
}
