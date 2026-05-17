data "aws_route53_zone" "primary" {
  name = "filingsradar.com."
}

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
