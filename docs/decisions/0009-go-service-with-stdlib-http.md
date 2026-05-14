# 0009. Go service uses stdlib net/http; no web framework

- **Status:** Accepted
- **Date:** 2026-05-13

## Context

The Go service serves the dashboard, exposes a small JSON API over the persistence layer, and streams new classifications to connected browsers via Server-Sent Events. The expected endpoint count for v0 is small: a health check, a few read endpoints over filings and classifications, an SSE stream, and (later) static asset serving for the dashboard.

The Go ecosystem has converged on a clear spectrum of HTTP options: stdlib `net/http`, router-only libraries (chi, gorilla/mux), full frameworks (echo, gin), and non-`http.Handler` runtimes (fiber on fasthttp). Go 1.22 (Feb 2024) added method+pattern routing to stdlib's `http.ServeMux`, closing most of the historical gap with router libraries.

The choice affects three things: dependency surface, conventions a future contributor needs to learn, and how much of the ecosystem (middleware, integration helpers) we can pull from off-the-shelf.

## Decision

The Go service uses **stdlib `net/http`** with no third-party HTTP framework or router. Routing uses Go 1.22+ method+pattern syntax. Middleware is plain function decoration around `http.HandlerFunc`. Templates use `html/template`. SSE is implemented directly against `http.ResponseWriter` (flushing after each event).

Where convenience helpers would be useful, we write small focused functions in the service module rather than pulling in a framework's version.

## Alternatives considered

### chi (go-chi/chi)

Rejected for v0. chi is the most respected router-only library in Go and would be the natural choice once the codebase grows past a single middleware chain or needs subrouter groups (e.g., `/api/v1/*` vs `/admin/*` with different middleware). For v0's ~5-10 endpoints with a single middleware chain, stdlib's improvements in Go 1.22 cover the same ground. Migration to chi if needed later is mechanical — chi accepts `http.HandlerFunc` directly.

### echo or gin (full frameworks)

Rejected. Both bring router, JSON binding, request validation, opinionated context types, and a middleware ecosystem. The cost is larger surface area, framework-specific conventions a contributor must learn, and a harder migration path off the framework if its assumptions ever clash with ours. The features they add (request binding, validation) we don't need at v0 scope.

### fiber (Express.js-style, built on fasthttp)

Rejected. Breaks compatibility with `http.Handler` — most Go ecosystem middleware, testing patterns, and integrations assume `net/http`. Performance gains from fasthttp aren't relevant to our workload (LLM-bound, not HTTP-bound). Niche fit.

### gorilla/mux

Rejected. Was the popular router pre-Go-1.22; now in maintenance mode. chi is the modern equivalent.

## Consequences

- **Easier:** Zero HTTP dependencies. The Go module imports nothing beyond stdlib, the DB driver, and whatever templating/SSE helpers we write. Boring is the goal.
- **Easier:** Anyone reading the code reads idiomatic Go HTTP, not a framework's DSL. Onboarding is "read the stdlib docs," not "read this framework's docs."
- **Easier:** Future migration to a router (chi) is mechanical because handlers are already plain `http.HandlerFunc`. No framework-specific code to unwind.
- **Harder:** Middleware chaining is hand-written rather than fluent. A pattern like `chain(logging, auth, rateLimit)(handler)` is a one-line helper we write once; chi's `r.Use(...)` is more ergonomic. For v0's single chain, the difference is negligible.
- **Harder:** Subrouter groups (different middleware for different URL prefixes) require manual composition. Not needed until the API surface diverges in v1+.
- **Accepted commitment:** Endpoints are stitched together by hand in a single `main.go` or routing module. When that file grows past ~150 lines or the routing logic becomes hard to read at a glance, that is the trigger to introduce chi.

## Migration triggers (when to revisit)

- Endpoint count exceeds ~30, or a single routing file becomes hard to scan
- Need subrouter groups with distinct middleware chains (e.g., authenticated admin endpoints alongside the public dashboard)
- Want pre-built middleware (CORS, structured logging, request ID propagation) rather than writing our own
- Team grows past one engineer (less context, more value in established framework conventions)

Until any of these is true, every Go web request goes through stdlib only.
