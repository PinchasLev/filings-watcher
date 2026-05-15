resource "aws_security_group" "host" {
  name        = "filings-watcher-host"
  description = "Phase 4 slice 1: SSH from operator IP only. Egress unrestricted."
  vpc_id      = data.aws_vpc.default.id

  tags = {
    Name = "filings-watcher-host-sg"
  }
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  security_group_id = aws_security_group.host.id
  description       = "SSH from operator IP"
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
  cidr_ipv4         = var.operator_ip
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.host.id
  description       = "Unrestricted egress for OS updates, SSM, app traffic"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}
