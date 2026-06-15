variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "availability_zone" {
  description = "Availability zone for the EC2 instance (within aws_region)."
  type        = string
  default     = "us-east-1a"
}

variable "instance_type" {
  description = "EC2 instance type. Default is t4g.small (ARM Graviton, 2 vCPU, 2 GB RAM)."
  type        = string
  default     = "t4g.small"
}

variable "root_volume_gb" {
  description = "Root EBS volume size in GB."
  type        = number
  default     = 20
}

variable "data_volume_gb" {
  description = "Dedicated data EBS volume size in GB. Holds SQLite DB and Caddy ACME state; survives instance replacement. See ADR 0019."
  type        = number
  default     = 10
}

variable "app_user" {
  description = "OS user that owns /opt/filings-watcher and runs the application processes."
  type        = string
  default     = "filings"
}

variable "acme_email" {
  description = "Email address used by Caddy for ACME (Let's Encrypt) registration and expiry notifications."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.acme_email))
    error_message = "acme_email must be a valid email address."
  }
}

variable "alarm_email" {
  description = "Email subscribed to the CloudWatch alarms SNS topic — the external dead-man's-switch (ADR 0031). Kept in gitignored tfvars like acme_email, not committed. The subscription must be confirmed via the emailed link before notifications deliver. The SNS topic fans out, so a later Discord/SMS bridge is an added subscription, not a change here."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alarm_email))
    error_message = "alarm_email must be a valid email address."
  }
}

variable "otel_collector_version" {
  description = "OpenTelemetry Collector Contrib version to install on the host (e.g., \"0.121.0\"). Operator-controlled so version bumps are an SSM rerun, not a code change. See ADR 0018."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", var.otel_collector_version))
    error_message = "otel_collector_version must be a semantic version like \"0.121.0\"."
  }
}
