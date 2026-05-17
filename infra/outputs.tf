output "instance_id" {
  description = "EC2 instance ID."
  value       = aws_instance.host.id
}

output "instance_arn" {
  description = "EC2 instance ARN."
  value       = aws_instance.host.arn
}

output "public_ip" {
  description = "Elastic IP attached to the host."
  value       = aws_eip.host.public_ip
}

output "ssm_session_command" {
  description = "Convenience: SSM Session Manager command (fallback access path)."
  value       = "aws ssm start-session --target ${aws_instance.host.id} --region ${var.aws_region}"
}
