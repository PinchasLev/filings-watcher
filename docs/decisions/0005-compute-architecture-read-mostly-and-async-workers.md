# 0005. Compute architecture — read-mostly service + async workers

- **Status:** Accepted
- **Date:** 2026-05-13

## Context

The project mixes operations with very different latency profiles:

- **Reads** (dashboard, search results, filing detail views) — tens of milliseconds; interactive request-response.
- **Classification of an 8-K** — a Claude call. 5–30 seconds typical, occasionally longer. Latency is determined by a third-party API and is not under our operational control.
- **Backfill** (e.g., classify multiple years of historical 8-Ks for a watchlist) — minutes to hours of bulk work.
- **Eventual user-triggered queries** (e.g., explanation of a classification, ad-hoc search across filings) — same latency profile as classification.

Inline classification on a user-facing HTTP request ties up a server worker for the duration of the Claude call. Under concurrency, an upstream latency spike propagates as thread-pool exhaustion downstream, taking the dashboard down with the classification path. This failure mode must be ruled out structurally, not by tuning.

V0 ships only the dashboard, which is read-mostly. The decision must not foreclose Tier 1 and Tier 2 features (interactive queries, explanations, ad-hoc searches) that depend on the same Claude-latency profile.

## Decision

Two-component compute architecture, separated by latency profile:

1. **Read-mostly service** (Go). Serves the dashboard, the SSE stream of new classifications, and any read-side API. Reads classification results from Postgres that were produced asynchronously. Never calls Claude inline on a user request.

2. **Async workers** (Python). Consume work from a queue (initial choice: SQS, but the abstraction is "a queue"). Do all LLM-driven work: classify new 8-Ks as they arrive from EDGAR, generate briefs, handle user-triggered query jobs. Write results back to Postgres. The service surfaces new results via SSE as they land.

User-triggered LLM operations follow the same pattern: the request enqueues a job, the user gets immediate acknowledgement and an SSE stream, and the worker pushes tokens (or a final result) back through the SSE channel. The user perceives "thinking..." within a frame and watches the answer build; no blank-spinner-for-15-seconds experience.

The deploy platform (App Runner, Fargate, Lambda, EC2, EKS) is **explicitly deferred** to a future ADR. The pattern decided here holds true regardless of platform.

## Rationale, in two parts

### Why never compute inline on a request

The general principle: **do not block synchronously on operations whose latency is not under your operational control.** Internal database lookups in your own region are operationally controlled — observable latency, addressable regressions — and inline is correct. Third-party API calls (LLMs, payment processors, external scrapers) are not operationally controlled — a 2 s → 30 s spike upstream produces thread-pool exhaustion downstream. Async-by-default for these calls eliminates the class of cascading failures induced by upstream latency variance.

### Why streaming makes long operations feel fast

The half-second-or-bust rule applies to *blank waits*. Users will tolerate seconds-long *visibly progressing* waits. Anthropic's streaming API emits tokens as they generate; piping those through SSE to the browser gives the user "answer appearing within ~500 ms, completes 8 s later" instead of "blank spinner for 8 s followed by a wall of text." The perceived UX is better than the equivalent synchronous response.

Streaming and queuing compose: for short interactive queries, stream tokens directly. For long batch operations (multi-document analysis, classifying a watchlist), use the queue with email/SSE/webhook completion notification.

## Alternatives considered

### Synchronous classification inline on user requests

Rejected. Adequate for single-user development load; fails under concurrent traffic the first time Claude latency spikes. The remediation later is the same architectural change as adopting it now, with the additional cost of a live migration. Adopt the target architecture once.

### All-in-one Python service (no separate read service)

Rejected. Python is well-suited to the agent layer but a weaker fit for the HTTP/SSE service profile under concurrency. Mixing fast-read and slow-write traffic in a single process violates the latency-separation principle established above: a queue of Claude calls on the same event loop as dashboard SSE connections allows slow operations to block fast ones.

### Lambda for everything, no persistent service

Rejected. Lambda's per-invocation model fits async workers but is a poor fit for SSE (requires long-lived connections, hits invocation timeouts, scales steady connections awkwardly). Combining a persistent service for the dashboard with Lambda workers is possible but adds two deploy targets where the two-component pattern adopted here covers the same ground more uniformly.

### Inline classification, with a circuit breaker on Claude latency

Rejected. Circuit breakers prevent cascading failure; they do not reduce operation latency. When the breaker opens, the dashboard returns errors for classification paths — the same user-visible failure mode as the unprotected sync case, with a different trigger.

### Skip the queue, use background tasks within the same process

Rejected. In-process background tasks (goroutines, asyncio tasks) lack retry semantics, observability, durability across process restarts, and horizontal scaling. A managed queue is the seam that provides these properties.

## Consequences

- **Easier:** Dashboard latency is bounded by Postgres + the Go service, both under direct operational control. Third-party latency variance cannot propagate to the dashboard path.
- **Easier:** Worker pool scales independently of read traffic. Backfill bursts and steady-state classification share the same pattern with different queue depths.
- **Easier:** Each component has a single clear responsibility. The Go service is "serve reads"; the Python worker is "do LLM work and write results." Failure modes are independently diagnosable.
- **Harder:** Two deploy targets instead of one. Two sets of logs to correlate when debugging an end-to-end flow. Mitigated by tracing both ends to LangSmith and structured logs in the service.
- **Harder:** A queue is operational surface. SQS is cheap and managed, but it's still a dependency to monitor (queue depth alarms, DLQ handling, retry policy).
- **Harder:** The SSE-from-worker-to-browser path requires the service to bridge the worker's output stream back to the user's connection. Concretely: worker writes intermediate state to Postgres or publishes to a pub/sub channel; service watches that channel and pushes to the SSE connection. One extra hop; well-trodden pattern.
- **Accepted commitment:** Any future feature that would naturally be "synchronous inline" must be evaluated against this decision. Easy in-process work stays in the service; anything that calls Claude, or otherwise depends on a third-party latency profile, goes through the queue.

## Deferred to future ADRs

- **Deploy platform** (App Runner / Fargate / Lambda / EC2 / EKS). Decision blocked on actually having running code and traffic shape to inform the choice. The pattern in this ADR is platform-agnostic.
- **Queue technology** (SQS / EventBridge / SNS / a self-hosted option). Default assumption is SQS for FIFO classification work plus EventBridge for scheduled triggers; locking that in deserves its own short ADR when we wire the first worker.
- **Streaming infrastructure** (SSE directly from the service vs. WebSocket vs. polling fallback). Deferred until the first interactive query feature lands and we know the actual UX needs.
