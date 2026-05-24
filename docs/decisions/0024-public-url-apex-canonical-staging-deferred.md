# 0024. Public URL: apex canonical, www redirect, staging deferred to multi-host

- **Status:** Accepted
- **Date:** 2026-05-24

## Context

The Caddy setup landed under [ADR 0016](0016-tls-acme-and-ca-pinning.md) initially served `staging.filingsradar.com` — chosen as a placeholder while the product surface was still iterating internally. With the home page now real (see [ADR 0023](0023-frontend-presentation-server-rendered-pico-via-cdn.md)) and the operator validating the deployed system end-to-end, the question of which URL is canonical needs an explicit answer.

A second pressure shaping this decision: the meaningful version of "staging" requires a separate host running a separate binary against a separate (or at least clearly-isolated) database. Single-host "staging" against the same binary and the same SQLite file is theater — it cannot validate schema changes, classifier upgrades, or orchestrator behavior in isolation from production. The illusion of pre-prod safety on a single-host deployment would be more harmful than no staging at all.

## Decision

Three public hostnames, all backed by the same EC2 host today, with clearly differentiated roles:

- **`filingsradar.com` (apex) — canonical product URL.** Serves the home page over HTTPS via Caddy reverse-proxying to the local Go service. This is the URL that appears in the resume, the README, and any external link.
- **`www.filingsradar.com` — 301 redirect to apex.** Visitors who instinctively type `www` land on the canonical URL; search engines canonicalize accordingly.
- **`staging.filingsradar.com` — 301 redirect to apex, with the DNS record reserved.** No real staging exists yet (no second host, no separate binary, no isolated database). The redirect avoids leaving a broken URL on the public internet; the DNS record reservation preserves the subdomain for the future. When a real staging environment is stood up, the staging Caddy block transitions from redirect to `reverse_proxy` against the staging host.

CSP joins the existing security-header set: `default-src 'self'`, `style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net` (Pico CDN), `script-src 'none'` (the page has none), `frame-ancestors 'none'`, `base-uri 'self'`, `form-action 'self'`.

**`/ops/*` routes are blocked from the public path at the Caddy layer.** The Go service exposes (or will expose) operator routes under `/ops/*` for at-a-glance health and operational state — per [[ops-surface-single-page-discipline]] this is bounded to at most one such page. Those routes must never be reachable from the public internet; tailnet-attached operators reach the Go service directly at `127.0.0.1:8080` (per [ADR 0014](0014-operator-access-via-mesh-vpn.md)'s mesh-VPN design) and see `/ops/*` normally. The Caddy block for `filingsradar.com` enforces this via a `handle /ops/*` directive that returns 404 *before* the request would otherwise be proxied to the Go service. The public-vs-tailnet split is therefore visible in two complementary places: (a) the Go service binds on all interfaces, but (b) Caddy refuses to proxy `/ops/*` for public hostnames. Adding a new ops route requires no Caddy change — the wildcard match keeps the boundary durable.

## Alternatives considered

### Apex-only, no www, no staging

Cleanest possible setup. Rejected because (1) `www.<domain>` is enough of a typing convention that returning a DNS resolution failure reads as missed polish to a visitor, and (2) the staging subdomain was already in place — converting it to a redirect costs three lines of Caddy and preserves the option for real staging without DNS churn later.

### www canonical, apex redirects to www

Older convention from the era when DNS apex CNAME limitations made `www` operationally easier. Both apex `A` and `ALIAS`-style records are well-supported now (Route 53 is using a plain `A` record against an Elastic IP), so the apex convention is the modern choice.

### Single-host staging — real pre-prod environment on the same machine

Rejected as theater. With the same Go binary, the same SQLite database, the same orchestrator instance, and the same OTel pipeline, `staging.filingsradar.com` and `filingsradar.com` would render identical content from identical data. The infrastructure would dress up an alias as an environment. Schema changes can't be safely validated, classifier upgrades can't be A/B-tested, infra changes can't be rehearsed — every change still hits prod the moment it's deployed.

The two-process variant of single-host staging (different ports, feature-flag-by-hostname) would partially address the binary-isolation gap but does nothing for data isolation. Operating two binaries to validate one feature flag is a heavy ongoing cost for limited value at this scale.

### Drop the staging DNS record entirely

Equally defensible. The deciding factor was that a DNS record costs nothing to keep, and reactivating staging later in a different shape (real multi-host) requires only changing the Caddy block — not re-provisioning DNS. The reservation is cheap optionality.

### Add rate limiting in Caddy

Deferred. Caddy's stdlib distribution does not include a rate-limit module; adding it requires a custom Caddy build via xcaddy, which would replace the official binary with a self-built one and add ongoing maintenance overhead (rebuild on Caddy releases, manage the custom plugin's compatibility, etc.). At the project's scale and threat model, the cost outweighs the benefit. When real bot traffic shows up in OTel telemetry — or when sustained scraper activity becomes operationally visible — that's the trigger to either add the custom Caddy build, front the deployment with AWS WAF, or shift to a CDN-fronted topology.

### Public flip via CDN (CloudFront, Cloudflare) in front of Caddy

Deferred. A CDN adds caching, edge filtering, DDoS protection, and a separate operational surface. The product is read-mostly over a small dataset; CDN value at v0 scale is mostly defensive (rate limiting, bot mitigation) and adds infrastructure ahead of need. Worth revisiting if (a) traffic grows past the host's serving capacity, (b) bots become a real signal in OTel telemetry, or (c) the cost of AWS data transfer out becomes a meaningful line item.

## Consequences

- **Easier:** the canonical URL is unambiguous. Resume, README, and external links all point at `https://filingsradar.com/`. Search engines see a clear canonical signal.
- **Easier:** visitors who type `www.filingsradar.com` or who follow a stale link to `staging.filingsradar.com` land correctly via 301. No dead URLs in the wild.
- **Easier:** the staging DNS reservation costs ~nothing and unblocks future real-staging work whenever a second host is provisioned. No DNS plumbing to redo at that point.
- **Easier:** CSP closes the "browser blindly trusts any script source" vector. The page has no scripts, so `script-src 'none'` is a sharper signal than allowlisting nothing.
- **Harder:** going public means the page is now in the wild. Any URL Caddy serves is reachable globally; future product changes can't assume a private audience. Discipline on what `/ops/*` exposes (per [[ops-surface-single-page-discipline]]) becomes operationally enforceable rather than aspirational.
- **Harder:** the `staging.filingsradar.com` redirect is mildly misleading — the URL works but doesn't represent a real environment. Worth a future cleanup of the redirect when real staging exists OR when the deprecation of the URL is communicated externally.
- **Accepted commitment:** rate limiting at the edge is a known gap. Acceptable today; revisit when telemetry shows real load.
- **Accepted commitment:** when a real staging environment is stood up, the `staging.filingsradar.com` Caddy block changes from redirect to `reverse_proxy`. The shape of the staging host (separate EC2, separate DB, sync strategy) is a future ADR.
