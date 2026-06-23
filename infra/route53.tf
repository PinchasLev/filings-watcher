data "aws_route53_zone" "primary" {
  name = "filingsradar.com."
}

resource "aws_route53_record" "apex" {
  zone_id = data.aws_route53_zone.primary.zone_id
  name    = "filingsradar.com"
  type    = "A"
  ttl     = 300
  records = [aws_eip.host.public_ip]
}

resource "aws_route53_record" "www" {
  zone_id = data.aws_route53_zone.primary.zone_id
  name    = "www.filingsradar.com"
  type    = "A"
  ttl     = 300
  records = [aws_eip.host.public_ip]
}

# staging.filingsradar.com is intentionally preserved against a future
# multi-host staging environment. Today the Caddy block for staging
# redirects to apex; when a real staging host exists this record will
# either change to point at it or remain on the prod host with a Caddy
# reverse_proxy directive bridging via the tailnet. See ADR 0024.
resource "aws_route53_record" "staging" {
  zone_id = data.aws_route53_zone.primary.zone_id
  name    = "staging.filingsradar.com"
  type    = "A"
  ttl     = 300
  records = [aws_eip.host.public_ip]
}

# Lock TLS issuance to Let's Encrypt — only this CA can issue certs for the
# apex or its subdomains. Belt-and-suspenders against compromised/rogue CAs.
resource "aws_route53_record" "caa" {
  zone_id = data.aws_route53_zone.primary.zone_id
  name    = "filingsradar.com"
  type    = "CAA"
  ttl     = 3600
  records = [
    "0 issue \"letsencrypt.org\"",
    "0 issuewild \"letsencrypt.org\"",
    "0 iodef \"mailto:${var.acme_email}\"",
  ]
}

# External liveness probe for the public web surface (ADR 0036). Route 53 checks
# https://filingsradar.com/health every 30s from multiple AWS edge locations and
# publishes HealthCheckStatus to CloudWatch; the alarm in cloudwatch.tf pages when
# it goes unhealthy. This tests the whole user path end to end — DNS -> Caddy -> TLS
# -> the Go app's /health handler — so it catches a web-service crash-loop, panic,
# OOM, expired cert, or DNS/host failure, including the case the host heartbeat
# cannot see: the app down while the box itself is healthy. /health is the Go
# server's lightweight liveness endpoint (no DB query). enable_sni so Caddy serves
# the apex cert.
resource "aws_route53_health_check" "site" {
  fqdn              = "filingsradar.com"
  port              = 443
  type              = "HTTPS"
  resource_path     = "/health"
  enable_sni        = true
  request_interval  = 30
  failure_threshold = 3

  tags = {
    Name = "filings-watcher-site-health"
  }
}
