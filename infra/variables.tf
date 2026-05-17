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
