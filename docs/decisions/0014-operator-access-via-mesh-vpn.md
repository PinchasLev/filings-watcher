# 0014. Operator access via mesh VPN

- **Status:** Accepted
- **Date:** 2026-05-15

## Context

[ADR 0013](0013-operational-observability-for-v0.md) commits to operator-facing endpoints (`/ops/runs`, `/ops/status`) that surface tick-level outcomes, error history, and run metadata. Until the domain is public, those endpoints can be lazily protected — basic-auth, an IP allowlist, anything that filters out the empty internet. The decision in this ADR is forced by the planned public flip: once `filingsradar.com` resolves to the host and is shared publicly, the `/ops/*` paths become reachable to anyone who guesses or discovers them.

The threat model after public flip:

- **Path-probing bots.** Continuous background scans for `/admin`, `/ops`, `/.env`, `/console`. These arrive within hours of the domain being indexed. They don't justify defending against; they justify making sure the surface they hit is empty.
- **Casual discovery.** Search engine cache, browser-history leak, a curious visitor noticing the path.
- **Credential compromise.** Phishing, stolen laptop, leaked password manager. Single-factor authentication is one bad day from defeated.

The threat model explicitly does *not* include a targeted attacker. Nothing behind `/ops/*` — read-only metadata about a public-data pipeline — motivates one. The bar is "no casual access," not "withstand a determined adversary." Reaching for OAuth, mTLS, or commercial identity providers over-invests in a problem v0 doesn't have.

The operator is one person with two devices (laptop, phone). Access from a third party's machine is explicitly not a requirement; the operator surface is for the operator alone.

## Decision

The host joins a Tailscale tailnet. The Go service exposes its operator endpoints only on the Tailscale interface (`100.x.y.z`); the public Caddy vhost has no route definition for `/ops/*` at all. From the public internet, requests to `/ops/*` receive the default Caddy 404, indistinguishable from any nonexistent path. From the operator's devices on the tailnet, `ops.filingsradar.com` resolves via Tailscale MagicDNS to the private address and serves the operator UI normally.

Concretely:

- `tailscaled` runs as a `systemd` service on the host, joined to a private tailnet owned by the operator's account.
- The Go service binds two listeners: `0.0.0.0:<public>` for public paths (served behind Caddy) and `100.x.y.z:<ops>` for `/ops/*`.
- Caddy's public vhost defines routes for the public surface only. There is no `/ops/*` route to bypass, mis-configure, or accidentally expose.
- DNS for `ops.filingsradar.com` resolves over MagicDNS within the tailnet; from the public internet, the name does not resolve to a routable address.
- The operator installs Tailscale on each device they intend to check from. Bookmarking `https://ops.filingsradar.com/status` from a tailnet-connected device is the entire daily workflow.

No credential is shared between operator and host. Access is authorized by the operator's Tailscale identity and the device key registered with the tailnet.

## Rationale

### Why mesh VPN, not authentication on a public endpoint

Authentication mechanisms on a public endpoint have an attack surface in the authentication code itself: a bug in basic-auth, a bypass in an OAuth proxy, a misconfigured rule that exposes the path. The mesh-VPN approach eliminates this class of risk by making the endpoint non-routable from the public internet. The operator surface is not "protected"; it is "not present" in the public reachability graph.

### Why Tailscale, not self-hosted WireGuard

Tailscale handles key distribution, device registration, MagicDNS, NAT traversal, and key rotation automatically. Self-hosted WireGuard requires managing all of these manually for every device. The free tier (3 users, 100 devices) covers the operator-of-one scenario indefinitely, and the dependency is on a product whose entire business is operating this layer reliably.

### Why this calibration, not stronger

A stronger answer — TLS client certificates, hardware tokens, an OIDC provider — would defend against threats the operator surface doesn't face. The data behind `/ops/*` is operational metadata about a read-only public-data pipeline. The cost of over-investment is real (configuration complexity, device setup friction, future-self confusion about why the system is the way it is); the benefit is hypothetical.

## Alternatives considered

### Caddy basic-auth on a public `/ops/*` path

Rejected. Adequate for the private-deploy phase before the public flip, inadequate once the domain is indexed. Single-factor, credential-in-browser-history, no defense against phishing or device compromise.

### IP allowlist via Caddy or AWS security group

Rejected as primary mechanism. Residential IPs rotate on most ISPs; phone on cellular has a different IP than phone on home wifi; travel breaks the allowlist entirely. Maintenance friction violates the daily-check workflow [ADR 0013](0013-operational-observability-for-v0.md) commits to.

### SSH tunnel to a localhost-bound `/ops/*`

Rejected. Strongest alternative — nothing exposed publicly at all — but the friction of opening a terminal and remembering a tunnel command for a daily check defeats the "below the operator's discipline floor" goal. Right answer if `/ops/*` were a quarterly-review surface.

### OAuth proxy (oauth2-proxy) against a GitHub identity

Rejected. Reasonable mid-weight option; introduces a second process to operate, a registered OAuth app, and a session-cookie domain to manage. Solves a problem (multi-user, audited access) the v0 operator surface doesn't have.

### Client certificates (mTLS)

Rejected. Browser certificate UX remains awful; managing per-device certs across laptop and phone is more friction than installing a Tailscale client. Comparable security with worse ergonomics.

### AWS-native private connectivity (Client VPN, Session Manager)

Rejected. AWS Client VPN has a minimum monthly cost (~$72/month for the endpoint alone) that exceeds the entire v0 budget and provides capability the operator-of-one scenario doesn't need.

## Consequences

- **Easier:** The `/ops/*` paths have no public attack surface. Bot probing, search-engine indexing, and casual discovery cannot reach them — there is no endpoint to reach.
- **Easier:** Operator authentication is identity-based and managed by Tailscale, not credential-based and managed by the operator. No password rotation, no shared secret to revoke.
- **Easier:** Adding a second operator device (a tablet, a backup laptop) is a Tailscale install + login, not a configuration change on the host.
- **Harder:** Tailscale is a vendor dependency. Outage of the Tailscale control plane temporarily prevents new device authorization; existing peer-to-peer connections continue working.
- **Harder:** One more daemon to run on the host (`tailscaled`). Low operational weight, but real.
- **Accepted commitment:** Operator access from devices not on the tailnet is impossible. There is no break-glass credential. The operator commits to maintaining a Tailscale-connected device.
- **Accepted commitment:** When operator count, audit needs, or scale-out requirements grow, this decision is revisited. The mesh-VPN answer is right for one operator and two devices; it is not the right answer for a team of five with rotating on-call.

## Deferred

- **Multi-operator access and audit logging.** Tailscale supports both; the operator-of-one scenario doesn't motivate configuring them. Revisit when ops grows beyond one person.
- **Replacement of Tailscale.** Self-hosted WireGuard, Cloudflare Tunnel, or a managed alternative may become preferable if vendor cost, vendor stability, or policy considerations change. The architectural commitment — `/ops/*` bound to a private interface — survives the choice of provider.
- **Break-glass access pattern.** A separate emergency path (e.g., SSH-tunnel fallback) for the case where Tailscale itself is unreachable. Deferred until the loss has been experienced once.
