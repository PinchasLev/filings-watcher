# 0018. Observability stack: OpenTelemetry-native, operator-controlled pipeline

- **Status:** Accepted
- **Date:** 2026-05-18

## Context

[ADR 0013](0013-operational-observability-for-v0.md) scopes operational observability for v0 narrowly: structured logs to stdout (captured by `journald`), one CloudWatch alarm on the unit's failed state, and operator endpoints (`/ops/runs`, `/ops/status`) for tick-level outcomes. That ADR explicitly defers continuous metrics, time-series storage, and dashboarding to v1.

That deferral leaves a forward-looking question unanswered: when continuous observability is needed, *what shape* does it take? The choices made now — which SDKs the application code uses, which agent runs on the host, where data is shipped — determine which doors remain open later. Vendor-specific instrumentation (Datadog's agent, CloudWatch's proprietary log format, AWS X-Ray's SDK) is cheap to adopt and expensive to leave: every instrumented call site becomes a re-write when the vendor decision is reversed. Vendor-neutral instrumentation is moderately more work up front and pays back the first time the backend changes — or the first time the data needs to flow somewhere the original vendor didn't anticipate.

The substrate is single-host today, with v1 commitments to remain single-host before any multi-host work. The observability surface should support both the current scale and future trajectories without rework.

## Decision

The observability stack is **OpenTelemetry-native end to end** and the pipeline is **operator-controlled**.

Concretely:

- **Application instrumentation uses OpenTelemetry SDKs.** Go service: `go.opentelemetry.io/otel` and its sub-modules. Python orchestrator: `opentelemetry-api` + `opentelemetry-sdk` + appropriate instrumentation packages. Both emit OTLP.
- **An OpenTelemetry Collector is the first hop.** All logs, metrics, and traces leave application processes via OTLP and pass through a Collector instance under operator control. The Collector is installed and configured as part of the substrate (not part of any vendor's installer script).
- **Storage backends are swappable via Collector exporters.** Choosing CloudWatch, Grafana Cloud, Honeycomb, ClickHouse, Loki/Mimir/Tempo, or a self-hosted stack is a Collector configuration change, not an application change.
- **Enrichment, redaction, sampling, and resource detection live in the Collector pipeline**, configured by the operator. Vendor-side enrichment (when a backend is chosen) supplements but does not replace the operator-controlled pipeline.
- **Telemetry can drive action, not just observation.** The pipeline supports feedback loops where observed signals influence behavior — whether that behavior is *inside* the application (adaptive rate limiting, dynamic shedding, circuit breaking), in an *adjacent* process (a controller that adjusts runtime parameters in response to observed load or latency), or in an *external* consumer (an alerting system that takes action, an agentic AI that triages and remediates, an automated rollback path triggered by post-deploy error rates). The Collector fans streams to action-taking consumers alongside long-term storage; observability does not stop at the dashboard.
- **Action consumers read telemetry locally, not by querying the backend.** Off-host storage is for dashboards, archival, and human-facing analysis. On-cluster decisions read the same data the Collector is *also* sending to storage, not from storage. Telemetry is fanned at the Collector — one source, multiple downstream consumers — so a control loop never round-trips through the backend to receive its own inputs. This keeps action paths low-latency, low-cost, and resilient to backend outages or rate limits.
- **Instrumentation is cost-aware and thoughtful, not exhaustive.** Signals get added when they inform a decision or detect a concrete failure mode. Cardinality is bounded — no per-request-ID labels, no unbounded user-identifier attributes on metrics, no debug-verbosity logging in production paths. Sampling rates and retention windows are set deliberately. The cost of the observability pipeline itself is monitored as a first-class concern, the same way AWS spend is.
- **Observability data remains accessible to derivative tools.** Logs, metrics, and traces are exposed in standard formats (OTLP, OpenMetrics, structured-JSON logs) through interfaces the operator controls — direct OTLP consumers, file exports, or backend query APIs that are not gated by a vendor's billing model. This preserves the ability to route telemetry into ML and AI analysis tools, custom anomaly detectors, archival pipelines, and agentic systems that reason about the running system's behavior.

ADR 0013's v0 surface (one CloudWatch alarm, journald-captured stdout logs) remains valid as the *starting point*. This ADR sets the *trajectory*: any observability surface added after v0 conforms to the OTel-native, operator-controlled commitment.

## Rationale

### Why OpenTelemetry-native instrumentation

OpenTelemetry is the industry's vendor-neutral standard for telemetry instrumentation, with first-class support in every relevant runtime (Go, Python, JVM, .NET, Node, Ruby, Rust) and import paths in every meaningful backend (Datadog, Honeycomb, Grafana, Splunk, New Relic, AWS, GCP, Azure, ClickHouse-backed stacks). Choosing OTel makes the application-side commitment durable across backend changes that this project will likely make as the system grows past the free tiers.

The alternative — committing to a vendor's SDK on day one — couples application code to that vendor. Switching later is a rewrite that touches every instrumentation point. The cost of OTel-native instrumentation is marginal: APIs are stable, ergonomics are reasonable, ecosystem maturity is high.

### Why an operator-controlled Collector as first hop

The Collector is the *control plane* of an observability pipeline. Decisions about which attributes to keep, which spans to sample, how to redact PII, how to enrich with deployment metadata, and how to route data to which backend live there. When a vendor's agent is the first hop, those decisions live in the vendor's configuration model, which is necessarily backend-coupled and often proprietary.

The OpenTelemetry Collector — an Apache-licensed project under the CNCF — gives a portable, declarative pipeline that survives vendor changes. The same Collector configuration can ship to CloudWatch today and to Honeycomb tomorrow with only the `exporters` block changed. Pipeline behavior (sampling, enrichment, redaction) is portable; backend choice is replaceable.

### Why storage-backend independence is worth preserving

Storage backends differ on cost structure (per-host pricing, per-ingest pricing, retention tiers), query model (full-text search vs columnar metrics vs span-graph traces), retention defaults, alerting capabilities, and integration with adjacent tooling. The right choice at v0 traffic (likely free-tier Grafana Cloud or self-hosted, or CloudWatch's existing budget envelope) is not necessarily the right choice at production traffic. Decoupling the application from the backend lets the choice change without code churn.

### Why telemetry-to-action feedback loops are called out explicitly

Most observability architectures treat the pipeline as a one-way export from the application to a dashboard, where humans read and decide. The pattern this ADR commits to is more general: telemetry is a substrate that other systems — internal control logic, sibling processes, external automation, AI agents — can both consume and act on.

Concrete examples within reach for this project: an orchestrator that throttles its ingestion rate when classifier latency rises, a service that sheds load when error rates spike, a post-deploy controller that triggers an automatic rollback when error rates exceed a threshold within a window, an agentic system that ingests recent logs and traces to triage an incident and recommend or take a remediation step.

This is structurally different from one-way export and benefits from being a stated capability of the pipeline rather than an after-the-fact bolt-on. The Collector's routing processor and OTLP-receiving deployment patterns make multi-consumer fanning natural; designing for action consumers from the start means they can land without re-architecting the observability surface.

### Why cost-aware instrumentation

Observability cost scales with cardinality, retention, and verbosity. A single high-cardinality label on a hot metric, an over-attributed trace, or a verbose log path in a request handler can each shift monthly cost by an order of magnitude with no proportional benefit. Reversing those choices after the fact is harder than picking them carefully up front.

The Collector is the right enforcement point. Cardinality reduction, attribute dropping, span sampling, and per-signal ingest caps belong in the operator-controlled pipeline, not in application code. This keeps cost discipline portable across backends and reversible without changing instrumented call sites.

Each new signal carries a lightweight justification: what decision does it inform, or what failure does it detect? Signals without an answer do not ship.

### Why action consumers must not round-trip through storage

If on-cluster decisions had to query the storage backend to read their inputs, every action loop would inherit the backend's latency, the backend's availability, and the backend's query costs. A circuit breaker deciding whether to trip on p99 latency cannot afford a 200 ms HTTP round-trip. An automated rollback path triggered by post-deploy error rates must not be unavailable when the observability backend itself is degraded. An agentic system triaging an incident should not be paying per query to fetch the same data the application produced moments ago.

The Collector's multi-output fan-out is the architectural answer. One source — telemetry leaving the application — splits into multiple downstream consumers. Storage gets a copy for dashboards and historical analysis. Local consumers (control loops, sidecar agents, in-cluster automation) get the same stream, fresh, with no round-trip and no dependency on the backend being available, cheap, or fast enough for the action loop's timing budget.

This is the property that makes off-host storage compatible with on-cluster decisioning: the *storage* layer is off-host; the *signal* layer is wherever any consumer needs it to be.

### Why derivative-tool accessibility is structural, not incidental

Telemetry is not just for human-facing dashboards anymore. It increasingly feeds anomaly detection, AI-assisted triage, pattern discovery in logs and traces, and agentic systems that observe service behavior to make or recommend decisions. Vendor-locked observability stacks frequently gate that access: proprietary storage formats, API rate limits on extraction, billing models that charge for re-reading your own data, server-side sampling that drops information before it reaches any consumer.

The operator-controlled Collector pattern keeps the data accessible by construction. A new derivative consumer — an ML pipeline, an AI-driven analyzer, a future tool that doesn't yet exist — is added as another OTLP consumer or another Collector exporter, without restructuring the pipeline or renegotiating with a vendor. The cost of recovering this access after committing to a vendor that gates it is the same cost as the vendor-neutrality decision itself: a full re-instrumentation or expensive data-extraction tooling.

### Why this ADR is not "just use the CloudWatch agent"

CloudWatch's agent is convenient and operationally cheap inside an AWS environment. It is also a one-way ticket: logs are stored in CloudWatch's format, metrics use CloudWatch's namespaces, and migrating off requires re-instrumenting every signal. The operator-controlled OTel Collector can *export to CloudWatch* (and likely will, initially) while keeping the application side, the pipeline shape, and the future-migration story intact.

## Alternatives considered

### Vendor-specific SDK + agent (Datadog, New Relic, Splunk, etc.)

Rejected. Couples application code and operational substrate to a single backend. Migration cost is unbounded. Cost structures favor the vendor at scale.

### CloudWatch-only: stdout → CloudWatch Logs Agent → CloudWatch metrics from a CWAgent config

Rejected as a long-term architecture; acceptable as a *destination* for the OTel Collector. CloudWatch's formats and idioms are AWS-specific; the application code should not know about them. The Collector mediates.

### Prometheus pull + Loki + Tempo, self-hosted on the host

Rejected for v0+ scale. Self-hosting the full triplet (Prometheus, Loki, Tempo, plus a dashboard layer) on a single t4g.small competes with application workload for resources, requires retention/sizing decisions the operator should not be making at v0 scale, and adds operational surface (backup, version upgrades, query tuning) the project does not need. The OTel Collector preserves the option to push *to* a self-hosted stack later without needing one now.

### Deferred decision: pick observability shape when we get there

Rejected. The shape determines application-side instrumentation choices. Picking the wrong instrumentation now and discovering it when observability is implemented means rewriting every instrumented call site. The operational substrate questions (which Collector distribution, which backend) are reasonable to defer; the instrumentation commitment is not.

## Consequences

- **Easier:** Adding instrumentation to either the Go service or the Python orchestrator is a well-trodden path with OTel SDKs. The exporter target is configured via environment variables (`OTEL_EXPORTER_OTLP_ENDPOINT`, etc.), no vendor lock at the SDK level.
- **Easier:** Switching backends as the project grows past free tiers or hits cost/feature limits is a Collector configuration change. The application is unaffected.
- **Easier:** Metrics that should drive runtime behavior can be exposed for that purpose without restructuring the observability surface.
- **Harder:** The first observability surface carries the up-front cost of running an OTel Collector — Collector deployment, base configuration, at least one exporter, application-side OTel SDK wiring. This is more substrate than "install the CloudWatch agent" would be.
- **Harder:** Sampling, redaction, and resource detection are operator responsibilities encoded in the Collector pipeline, not delegated to a vendor's defaults. Worth the cost; not free.
- **Accepted commitment:** All new instrumentation uses OTel SDKs. Vendor SDKs that emit only vendor-specific telemetry are out of scope.
- **Accepted commitment:** The OTel Collector is part of the substrate. Operational responsibility (config, upgrades, observability of the Collector itself) is non-zero.
- **Accepted commitment:** Observability cost is monitored alongside AWS cost. Adding a new instrumented signal requires a lightweight justification: what decision does this inform, or what failure does it detect? Signals that fail the check do not ship.

## Deferred

- **Collector distribution.** OTel Collector Contrib (kitchen-sink) vs OTel Collector Core (minimal) vs a custom build via `ocb` (OpenTelemetry Collector Builder). Contrib is the conventional starting point; the choice becomes meaningful when the set of receivers/processors/exporters stabilizes.
- **Deployment shape.** Sidecar (Collector per host, scrapes local services and exports out) vs gateway (a separate Collector node aggregating from many hosts). At single-host scale these collapse to the same thing; the choice becomes meaningful with the v2 multi-host trigger.
- **Storage backend.** No commitment in this ADR. The first iteration will most likely use CloudWatch (already in the budget envelope) or Grafana Cloud's free tier; the choice is reversible by exporter swap.
- **Specific signals.** What gets instrumented (which spans, which metrics, which log fields) is later work. This ADR commits to the *how*, not the *what*.
- **Observability cost monitoring tooling.** Tracking ingest volume per backend, query cost, alerts on spend — designed once a backend is in place to monitor.

## Relation to ADR 0013

[ADR 0013](0013-operational-observability-for-v0.md) defines the v0 observability surface. This ADR does not supersede it; the v0 surface (single CloudWatch alarm, journald-captured stdout) remains in place. This ADR commits the *next* observability surface to be OTel-native rather than ad hoc, so that work has a defined direction rather than a fresh decision.
