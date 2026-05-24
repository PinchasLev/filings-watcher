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
