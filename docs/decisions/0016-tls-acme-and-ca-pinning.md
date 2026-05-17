# 0016. TLS, ACME, and CA pinning for v0

- **Status:** Accepted
- **Date:** 2026-05-17

## Context

The v0 deploy substrate ([ADR 0015](0015-deploy-pipeline-and-iac-for-v0.md)) provisions a single EC2 host serving a public surface on a custom domain. The host needs a TLS certificate trusted by mainstream browsers, automatic renewal, and a defensible posture against issuance abuse. The portfolio surface does not (yet) handle credentials, payments, or otherwise high-value data — the threat model is the public internet baseline, not a targeted adversary.

An ACM certificate covering the apex and wildcard for both project domains has been pre-staged in `us-east-1`. ACM private keys cannot be exported, so the cert is only usable when paired with an AWS-managed TLS-terminating endpoint (ALB, CloudFront, API Gateway). The deploy substrate is a single EC2 host running Caddy; no such endpoint is in scope for v0.

## Decision

TLS is terminated on the EC2 host by Caddy. Certificates are issued and renewed automatically by Let's Encrypt via the ACME HTTP-01 challenge. A DNS CAA record on the apex restricts issuance to Let's Encrypt. The pre-staged ACM cert is retained but unused until a CDN layer is introduced.

Concretely:

- Caddy listens on 80 and 443. Port 80 responds only to ACME challenges and 301-redirects everything else to HTTPS. Port 443 serves the application with TLS.
- The host's security group allows 80/tcp and 443/tcp from `0.0.0.0/0`.
- A Route53 `CAA` record on `filingsradar.com` permits issuance only by `letsencrypt.org` (both `issue` and `issuewild`), with an `iodef` mailto for violation reports.
- Caddy adds `Strict-Transport-Security: max-age=31536000; includeSubDomains` (1 year, subdomains covered, no preload submission) and a small set of hardening response headers (`X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`).
- The Caddyfile is owned by Terraform via `user_data.sh.tpl`; configuration changes that need to ride with infra changes land there. Operator-level reloads (`systemctl reload caddy`) are done over SSM for fast iteration.

## Rationale

### Why Caddy + Let's Encrypt, not ACM + CloudFront

ACM is the AWS-native answer when the TLS termination point is an AWS-managed endpoint. Putting a CloudFront distribution in front of a single t4g.small for one operator's portfolio site adds cost (~$0–10/mo depending on traffic, plus complexity around invalidations and origin shielding) for marginal benefit at this scale. Caddy's auto-TLS is one configuration directive, free, and battle-tested across millions of deployments.

The ACM cert is retained — it costs $0 to keep, auto-renews, and slots in cleanly if CloudFront becomes warranted later (high traffic, geographic distribution, DDoS shielding).

### Why HTTP-01, not DNS-01

HTTP-01 requires port 80 open to the world; DNS-01 requires Caddy to have IAM permissions on Route53 and a custom Caddy build with the `caddy-dns/route53` module via `xcaddy`. For one host serving one domain, the operational simplicity of stock Caddy + open port 80 dominates. Port 80 is "open" but exposes only the ACME challenge endpoint and an HTTPS redirect — neither serves application content.

DNS-01 becomes preferable when port 80 cannot be opened for policy reasons or when wildcards must be issued (HTTP-01 cannot validate wildcards). Neither condition holds here.

### Why CAA pinning

The CAA record costs nothing to maintain and forecloses an entire attack class: a compromised or coerced CA issuing certificates for `filingsradar.com` to an attacker. With CAA, even a fully compromised non-Let's-Encrypt CA cannot mint a valid cert for the domain. Browser-trusted CAs honor CAA by RFC; verification is automatic.

### Why HSTS without preload

HSTS protects returning visitors from HTTP-downgrade attacks. Preload extends that protection to first-time visitors by shipping the domain in browsers' hardcoded HTTPS-only list. Preload is a hard-to-reverse commitment (removal takes weeks-to-months of browser update cycles after the preload directive is stripped and a removal request is submitted) and is justified for domains where first-visit interception would cost something material. For the v0 portfolio surface, the first-visit gap is theoretical — a targeted MITM during a stranger's first visit to a site that handles no credentials. The cost-benefit tilts toward keeping reversibility.

`includeSubDomains` is included so future subdomains (`ops`, `app`, etc.) are covered without re-headering each one. The 1-year max-age matches Caddy's default and is sufficient for ordinary browser caching.

### Why standard hardening headers

`X-Content-Type-Options: nosniff` prevents browsers from inferring MIME types and executing content as a different type than served. `Referrer-Policy: strict-origin-when-cross-origin` reduces leakage of URL paths to third-party sites users navigate to. `Permissions-Policy` denies access to sensitive browser APIs (geolocation, camera, microphone) the service has no reason to request. Each is one line of configuration; collectively they raise the cost of an XSS or third-party-leak bug.

## Alternatives considered

### ACM cert behind CloudFront

Rejected for v0. Adds a CDN layer to solve a problem (cert sourcing) that Caddy solves for free. CloudFront becomes interesting when there is real traffic warranting geographic distribution, DDoS absorption, or aggressive caching — none of which apply to a single-operator portfolio surface. The ACM cert is retained against that future.

### DNS-01 challenge

Rejected for v0. Requires a custom Caddy build (`xcaddy build` with `caddy-dns/route53`), a Route53 IAM policy attached to the host role, and slightly more complex failure-mode debugging (AWS API errors vs HTTP request logs). Right answer if port 80 had to stay closed for policy reasons or if wildcards needed automation.

### HSTS preload

Deferred. The first-visit MITM scenario it closes is real but vanishingly unlikely against a portfolio domain with no announced URL. Preload is opt-in later (one header change plus a form submission); the inverse (un-preload) is slow and operationally painful. The commitment-to-benefit ratio doesn't favor preload until the service handles something material on first visit.

### Self-signed certs / private CA

Rejected. The public surface needs browser-trusted certs by definition. Self-signed is right for the tailnet operator surface (which Tailscale handles transparently) — wrong for the public domain.

### Manual cert management

Rejected. Renewal cadence (90 days for Let's Encrypt) plus the operator-of-one staffing model makes any non-automatic renewal a guaranteed future outage. Caddy's ACME integration is the operational baseline.

## Consequences

- **Easier:** Cert issuance and renewal happen automatically; no operator action required after the first apply. CAA forecloses cross-CA issuance abuse at the DNS level.
- **Easier:** Public surface ships with HSTS, hardening headers, and TLS 1.2+ (Caddy default) without per-route configuration.
- **Harder:** Port 80 is exposed to the public internet. Caddy's `:80` handling is bounded to ACME and redirects, but the surface exists. A bug in Caddy's HTTP-01 handler is a real (if very small) attack vector.
- **Harder:** Cert issuance depends on Let's Encrypt's availability and the host's outbound and inbound network. An ACME outage or a misconfigured CAA delays issuance until both sides recover.
- **Accepted commitment:** Caddy is now a critical-path dependency. Its operational behavior (config reloads, log format, ACME state on disk) is part of the v0 substrate's contract.
- **Accepted commitment:** The ACME email registered with Let's Encrypt must remain a real, monitored address. Renewal warnings and CA notifications route there; ignoring them risks silent cert expiry.

## Deferred

- **HSTS preload submission.** Revisit when the service handles credentials, payments, or other material first-visit data, or when an established audience justifies the commitment.
- **CloudFront / CDN layer with the ACM cert.** Revisit when traffic, geographic latency, or DDoS exposure motivates it.
- **DNS-01 challenge.** Revisit if port 80 must close for policy reasons or wildcard automation becomes needed.
- **OCSP stapling tuning, TLS 1.3-only enforcement, custom cipher suites.** Caddy defaults are reasonable; tightening these is a phase-5 hardening task once there's evidence the defaults are insufficient.
