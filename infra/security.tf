resource "aws_security_group" "host" {
  name        = "filings-watcher-host"
  description = "Phase 4 slice 3: HTTP/HTTPS public ingress for Caddy; operator access via Tailscale + SSM."
  vpc_id      = data.aws_vpc.default.id

  tags = {
    Name = "filings-watcher-host-sg"
  }
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.host.id
  description       = "HTTP for ACME HTTP-01 challenge and HTTPS redirect to 443"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.host.id
  description       = "HTTPS for the public web surface served by Caddy"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.host.id
  description       = "Unrestricted egress for OS updates, SSM, Tailscale, app traffic"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}
