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

variable "operator_ip" {
  description = "Operator's public IP in CIDR form (e.g., 203.0.113.42/32). Only this address can SSH to the host."
  type        = string

  validation {
    condition     = can(regex("^[0-9.]+/(8|16|24|32)$", var.operator_ip))
    error_message = "operator_ip must be a single CIDR ending in /8, /16, /24, or /32."
  }
}

variable "ssh_public_key" {
  description = "SSH public key (ed25519) authorized for the ec2-user account."
  type        = string

  validation {
    condition     = can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) ", var.ssh_public_key))
    error_message = "ssh_public_key must start with a recognized key-type prefix (ssh-ed25519, ssh-rsa, ecdsa-sha2-nistp256)."
  }
}

variable "app_user" {
  description = "OS user that owns /opt/filings-watcher and runs the application processes."
  type        = string
  default     = "filings"
}
