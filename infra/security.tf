resource "aws_security_group" "host" {
  name        = "filings-watcher-host"
  description = "Phase 4 slice 2: no public ingress. Host reachable via Tailscale + SSM only."
  vpc_id      = data.aws_vpc.default.id

  tags = {
    Name = "filings-watcher-host-sg"
  }
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.host.id
  description       = "Unrestricted egress for OS updates, SSM, Tailscale, app traffic"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}
