# 0013. Operational observability for v0

- **Status:** Accepted
- **Date:** 2026-05-15

## Context

[ADR 0012](0012-ingestion-cadence-periodic-v0-push-v1.md) commits the v0 pipeline to a tuning loop: tick interval, run timeout, classification concurrency, and rate-limit budgets are externalized as configuration so they can be adjusted against observed behavior. That commitment is only honorable if the operator can *see* the behavior in question. Without visibility, the tunables become folklore — adjusted on hunches, defended by the absence of contradicting evidence.

The operational scale is small. v0 is one process running on one host, classifying tens to hundreds of filings per tick. The operator is one person. The questions that need answering daily are correspondingly small: *is the pipeline alive, what did the last day look like, why did this specific filing fail, what is my Claude spend trending toward.* The observability surface should match this scale — large enough to answer those questions, small enough that building it costs hours, not weeks.

A larger stack (Prometheus, Grafana, OpenTelemetry, dashboard infrastructure) is the right answer for v1's split-process pipeline with queue-depth time series and worker-pool utilization curves. It is the wrong answer for v0, where the cardinality of operational questions is small enough to handle with point-in-time queries against a SQLite table and a JSON log feed.

## Decision

Four capture surfaces and four access mechanisms. The capture surfaces emit data; the access mechanisms put that data in front of the operator.

**Capture surfaces:**

1. **Structured `systemd-journald` logs.** The orchestrator emits JSON lines for: tick start, tick end (with summary fields), each filing fetched, each classification attempted (success or failure), each external API call (with duration and status). `journald` captures these for free, retains per host configuration (default ~30 days), and is queryable via `journalctl -u orchestrator -o json`.

2. **A `runs` table in SQLite.** One row inserted per tick, written as the orchestrator's last action before exit. Captured fields: `started_at`, `ended_at`, `tick_seq`, `outcome` (`completed` / `timed_out` / `errored`), `filings_found`, `filings_classified`, `filings_failed`, `edgar_calls`, `anthropic_calls`, `anthropic_tokens_in`, `anthropic_tokens_out`, `error_summary`. Three lines of SQL answer "what did the last 24 hours look like" or "what's my token consumption trend."

3. **LangSmith for Claude calls.** The existing LangGraph integration in the classifier traces every Claude call's prompt, completion, tokens, latency, and inferred cost. Captured on the LangSmith account already provisioned for the project; no additional integration work for v0.

4. **CloudWatch alarm on `systemctl is-failed`.** A single alarm watches the orchestrator's unit state via a small CloudWatch agent or a periodic script; transitions to failed state send email. This is the only proactive alert in v0.

**Access mechanisms:**

1. **`/ops/runs` JSON endpoint** on the Go read service. Accepts `since=<duration>` and `limit=<N>`. Returns the `runs` table contents as JSON. The operator's primary daily-check surface, queryable from anywhere with `curl` and `jq`.

2. **`/ops/status` HTML page** on the Go read service. Server-rendered, no JavaScript, ~50 lines of Go template. Displays at-a-glance: last tick time and outcome, 24-hour tick count and success ratio, total filings classified to date, most recent error excerpt. The "is it working?" answer rendered without a CLI.

3. **SSH + `journalctl`** for incident drill-down. When `/ops/status` or `/ops/runs` indicates something failed, SSH to the host and grep the structured journal for the specific accession number or error class. Required infrequently; powerful when required.

4. **LangSmith web UI** for classifier behavior. Already accessible at langsmith.com against the existing account.

Both `/ops/runs` and `/ops/status` are bound to a private interface and unreachable from the public internet. The access mechanism — mesh VPN, with the operator's devices on a private tailnet — is specified in [ADR 0014](0014-operator-access-via-mesh-vpn.md).

The operator's daily workflow, as a contract this ADR commits to supporting: *open `/ops/status` in the morning; if anything looks wrong, hit `/ops/runs` for the tick history; if a specific filing is suspect, SSH and grep the journal; if classifier behavior is the question, open LangSmith.* Four steps, each escalating, each independent.

## Rationale

### Why this scale, not larger

A metrics-time-series-dashboards stack solves problems v0 doesn't have: high cardinality (one process, not a fleet), continuous high-rate events (a tick every 15 minutes, not requests-per-second), and operator distance from the system (single owner, not on-call rotation). The cost is not the dashboards themselves — Grafana is free — it is the operational surface: a metrics endpoint to maintain, a scraper to run, retention policies to tune, alerts to define, dashboards to keep current. Each of those is a small investment that compounds into "the observability stack is now a thing we own." v0 doesn't need that yet, and v1's split-process design is a more honest justification for it.

### Why this scale, not smaller

"Just read the logs" is the seductive minimum, and it fails the first time a question requires aggregation. "How many filings did we classify last week?" is one `SELECT` against the `runs` table and infeasible against the journal without scripting. The `runs` table is the smallest aggregation surface that turns a stream of events into queryable answers, and it costs one table.

### Why `/ops/status` HTML, not "just the JSON"

The HTML status page is the difference between "the operator checks the system" and "the operator doesn't check the system." JSON requires a tool, an open terminal, and remembered query strings. A bookmark to a URL costs zero friction and gets opened. Daily visibility depends on the friction being below the operator's discipline floor — and that floor varies day to day.

## Alternatives considered

### CloudWatch Logs agent shipping journal output

Rejected for v0. The agent itself is free, but routing logs into CloudWatch introduces a per-GB ingestion cost, a retention configuration, and a second place to look for what `journalctl` already shows. Worth revisiting when v1 introduces multiple processes whose logs need correlated viewing.

### Prometheus metrics endpoint + Grafana dashboard

Deferred to v1. The metrics that would justify Prometheus — queue depth over time, worker pool utilization, p95 latency histograms — exist in v1's pipeline, not v0's. Adding Prometheus to v0 means instrumenting metrics that read flat or constant and rendering them on a dashboard the operator doesn't have a reason to open.

### Per-filing detailed events into the `runs` table

Rejected. The `runs` table is per-tick summary, not per-filing detail; per-filing data lives in the existing classification tables and the journal. Conflating them turns the `runs` table into an event log with redundant data and complicates the per-tick summary queries it exists for.

### No `/ops/*` endpoints; require SSH for everything

Rejected. SSH-for-everything works for incident response and fails as a daily check, because the daily-check workflow needs to live below the operator's friction floor. The status page is the difference between observability being a habit and observability being a remediation step.

### Pushing observability to a managed service from day 1 (Datadog, Honeycomb, etc.)

Rejected for v0. Managed observability has real engineering leverage at scale and a hard-to-justify monthly bill at v0's. Migrating from a journal + SQLite + LangSmith stack to a managed service later is a few days' work; building against one prematurely is a recurring expense for capability that isn't yet used.

## Consequences

- **Easier:** Daily operational checks are a bookmarked URL. The tuning loop [ADR 0012](0012-ingestion-cadence-periodic-v0-push-v1.md) commits to is mechanically supported — open the page, read the numbers, decide whether to adjust a configured value.
- **Easier:** Cost-tracking is one LangSmith dashboard. No spreadsheet, no manual log-grepping for token usage.
- **Easier:** Incident drill-down has a well-defined sequence (status page → runs table → journal grep → LangSmith) rather than ad-hoc forensics.
- **Harder:** Two new endpoints (`/ops/status`, `/ops/runs`) on the Go service to maintain, with their access wrapper. Modest surface, but real.
- **Harder:** The `runs` table is a new schema obligation; migrations need to handle it. (At v0's SQLite-on-single-host scale this is a `CREATE TABLE IF NOT EXISTS` in the orchestrator's startup path, not a migration tool. v1 may revisit.)
- **Accepted commitment:** Anything the orchestrator does that an operator might want to debug after the fact gets a structured log line. "Silent success" is a bug; "silent failure" is a worse bug.
- **Accepted commitment:** When v1 lands, this ADR is revisited. The four-surface stack is sufficient for v0's question shape; v1's question shape (queue depth, worker utilization, throughput over time) demands continuous measurement, not point-in-time queries.

## Deferred

- **v1 observability — queue-depth and worker-pool metrics.** Continuous measurement, time-series storage, and dashboard rendering become honest investments when the pipeline has continuous state worth measuring. Deferred until v1's split lands.
- **Alerting beyond unit-failure.** Threshold alarms on tick duration, error rate, queue depth, token budget consumption. Deferred until the data exists to set thresholds from, rather than guesses.
- **Log shipping and retention beyond systemd defaults.** Journal retention is per-host and bounded by disk; longer retention or off-host storage is a separate operational decision.
