# 0023. Frontend presentation: server-rendered HTML with Pico.css via CDN

- **Status:** Accepted
- **Date:** 2026-05-21

## Context

The Go read service serves the public filings UI ([the first user-facing surface of the system](../../service/internal/server/handlers_home.go)). Until this decision, the service only exposed JSON endpoints; the home page and any subsequent product pages are net-new presentation.

The shape of the data is read-mostly. Each page is a server-side query against the shared SQLite database, rendered into HTML for a browser visitor. There is no client-side interactivity that would justify a JS framework: filters are query parameters, links navigate, the server renders. The audience includes portfolio visitors who land on the URL without context, so the page needs to look respectable on first impression without depending on a long visual-polish iteration loop.

Earlier project decisions narrow the landscape:

- [ADR 0009](0009-go-service-with-stdlib-http.md) commits the service to stdlib `net/http` with Go 1.22+ pattern routing — no third-party HTTP framework. The "no third-party framework" discipline extends naturally to the presentation layer: introducing a JS framework, a build pipeline, or a heavyweight design system would re-litigate that decision.
- [The foundation-over-flash principle](../../README.md) governs *substrate* choices (observability, persistence, instrumentation); it is not a rule against making the user-facing presentation look modern.

## Decision

Server-rendered HTML with stdlib `html/template`, styled by [Pico.css](https://picocss.com/) 2.x loaded from jsdelivr's CDN. Templates are embedded into the Go binary via `//go:embed`.

Concretely:

- One `<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">` in the layout template.
- Pico is a **class-less framework**: it styles default semantic HTML (`h1`, `p`, `article`, `nav`, `table`, `button`, `form`, etc.) to a modern baseline. Templates write semantic HTML and avoid class-soup.
- Small page-specific tweaks live in an inline `<style>` block in the layout template (filter-chip styles, card spacing, badge colors). The inline overrides stay small; significant style departures would trigger a revisit of this ADR.
- No JavaScript. Filter state is a `?event=` query parameter; clicks navigate; the server re-renders.
- Templates are embedded via `//go:embed` into the binary so production deploys ship one artifact without a separate static-assets directory.

## Alternatives considered

### Inline CSS only (no external dependency)

Rejected. Pico provides a respectable visual baseline (typography, spacing, color, responsive defaults) with ~10 KB minified. Replicating that quality with inline CSS would mean ~150-200 lines of carefully-tuned style choices, and the result still wouldn't get free accessibility defaults that Pico's semantic-HTML approach gives away. The "self-contained, no CDN" property is valuable for offline-first applications; this is a public web product where the CDN dependency is acceptable.

### Tailwind CSS via CDN

Rejected. Tailwind is class-based: every element needs explicit utility classes (`<div class="flex items-center gap-4 px-4 py-2 ...">`). That produces dense templates and tightly couples markup to styling. Tailwind's class-soup pattern is well-loved by some teams and unloved by others, but the bigger issue is the philosophical mismatch: stdlib templates with class-less CSS keeps templates declarative; Tailwind would push styling into the template language.

The Tailwind CDN-only build also ships ~3 MB unless tree-shaken via a build step. The build step would re-introduce a Node/PostCSS pipeline this project deliberately avoids.

### A build pipeline (custom SCSS, Tailwind with JIT, esbuild, etc.)

Deferred. A real build pipeline adds Node tooling, a separate CI step, and a build artifact to manage. For a product whose v0 styling needs are "look respectable and stay out of the way," the CDN'd class-less framework is the right cost. If the design needs ever outgrow Pico — custom components, a brand identity, animations — that's the trigger to revisit and introduce a build pipeline.

### A frontend framework (React, Vue, Svelte, HTMX, Alpine)

Rejected. The data is read-mostly and the interactions are link-and-form. A SPA framework would add a runtime, a bundler, a router, hydration concerns, and a maintenance surface for properties the server-rendered HTML already provides for free. HTMX is the tempting middle ground (server-rendered HTML with declarative partial updates) but the current product doesn't need partial updates either; every meaningful interaction is a page load.

### Materialize, Bootstrap, Bulma, or another class-based framework

Rejected on the same grounds as Tailwind, with the additional drawback that these frameworks come with opinionated component libraries that out-shape the templates more than Pico does.

### A managed design system (e.g., a vendor's React component library)

Rejected. Same vendor-coupling argument that [ADR 0018](0018-observability-otel-native-operator-controlled.md) makes for observability backends: pick the thing that lets you swap later, not the thing that locks the application code in.

## Consequences

- **Easier:** the home page (and subsequent product pages) reach "looks respectable" with zero design effort. The visual baseline is good enough that the engineering story stays the focus when a portfolio visitor lands.
- **Easier:** templates stay close to semantic HTML. Reading a template tells you what the page *is*; styling lives in the layout's `<style>` block and Pico's defaults.
- **Easier:** swapping or upgrading Pico is a one-line change in the layout template. Pico 2.x → 3.x or to a different class-less framework (`new.css`, `simple.css`) is the same edit.
- **Harder:** the CDN dependency means the page styling depends on `jsdelivr.net` being reachable from the visitor's network. Pico-via-CDN is broadly stable but it is one external request the page would not make if we self-hosted the CSS.
- **Accepted commitment:** templates stay class-less by default. When a page-specific style is needed, it lives in the layout's inline `<style>` block, not as classes on every element. If the inline overrides grow past ~80 lines, that's the signal to either extract to a real stylesheet served from the service or revisit this ADR.
- **Accepted commitment:** no JavaScript in the rendered HTML at v0. Interactivity is query parameters and form submissions. The first JS would be the trigger to revisit.
- **Accepted commitment:** if a future page genuinely needs a build pipeline (custom design system, sophisticated component library), that's the moment for a new ADR covering the build tooling. Until then, the project keeps zero Node tooling.
