# 0005. Compute architecture — read-mostly service + async workers

- **Status:** Accepted
- **Date:** 2026-05-13

## Context

The project mixes operations with very different latency profiles:

- **Reads** (dashboard, search results, filing detail views) — should return in tens of milliseconds. The user is staring at a browser; standard half-second-or-bust rule applies.
- **Classification of an 8-K** — a Claude call. 5–30 seconds typical, occasionally longer. The latency is not under our control: third-party API, variable model load, retries.
- **Backfill** (e.g., classify five years of historical 8-Ks for a watchlist) — minutes to hours of bulk work.
- **Eventual user-triggered queries** (e.g., "explain this classification," "find filings matching X") — same Claude-call profile as classification: seconds, variable.

A single request-response service that does classification inline on a user's HTTP request would tie up server resources for the duration of the Claude call. Under any concurrency, this breaks: a latency spike on Claude's side propagates as thread-pool exhaustion on ours, and the dashboard goes down with the worker. This is the failure mode the architecture must structurally rule out.

The dashboard for v0 is read-mostly and could in principle ship without ever solving the slow-operation problem — but the design must not paint us into a corner once Tier 1 / Tier 2 features (interactive queries, "explain this," ad-hoc searches) arrive.

## Decision

Two-component compute architecture, separated by latency profile:

1. **Read-mostly service** (Go). Serves the dashboard, the SSE stream of new classifications, and any read-side API. Reads classification results from Postgres that were produced asynchronously. Never calls Claude inline on a user request.

2. **Async workers** (Python). Consume work from a queue (initial choice: SQS, but the abstraction is "a queue"). Do all LLM-driven work: classify new 8-Ks as they arrive from EDGAR, generate briefs, handle user-triggered query jobs. Write results back to Postgres. The service surfaces new results via SSE as they land.

User-triggered LLM operations follow the same pattern: the request enqueues a job, the user gets immediate acknowledgement and an SSE stream, and the worker pushes tokens (or a final result) back through the SSE channel. The user perceives "thinking..." within a frame and watches the answer build; no blank-spinner-for-15-seconds experience.

The deploy platform (App Runner, Fargate, Lambda, EC2, EKS) is **explicitly deferred** to a future ADR. The pattern decided here holds true regardless of platform.

## Rationale, in two parts

### Why never compute inline on a request

The rule, generalized: **do not block synchronously on operations whose latency you do not control.** Internal database lookups in your own region are under your operational control (milliseconds, you'd notice and address a regression) — inline is correct. Third-party API calls (LLMs, payment processors, external scrapers) are not — a latency spike from 2 s to 30 s on their side becomes thread-pool exhaustion on yours. Async-by-default for these calls protects against the entire class of "their bad day becomes our outage" failures.

### Why streaming makes long operations feel fast

The half-second-or-bust rule applies to *blank waits*. Users will tolerate seconds-long *visibly progressing* waits. Anthropic's streaming API emits tokens as they generate; piping those through SSE to the browser gives the user "answer appearing within ~500 ms, completes 8 s later" instead of "blank spinner for 8 s followed by a wall of text." The perceived UX is better than the equivalent synchronous response.

Streaming and queuing compose: for short interactive queries, stream tokens directly. For long batch operations (multi-document analysis, classifying a watchlist), use the queue with email/SSE/webhook completion notification.

## Alternatives considered

### Synchronous classification inline on user requests

Rejected. Convenient in v0 when traffic is just the developer hitting refresh; ruinous the first time anyone else hits the dashboard during a Claude latency spike. The fix later is the same work as doing it right now, plus the migration. Pay it once.

### All-in-one Python service (no separate read service)

Rejected. Python is the right tool for the agent layer but a weaker fit for HTTP/SSE under any meaningful concurrency. More importantly, mixing fast-read and slow-write traffic on the same process is exactly the failure mode we're protecting against — a queue of Claude calls on the same event loop as the dashboard's SSE connections is asking for slow operations to starve fast ones.

### Lambda for everything, no persistent service

Rejected. Lambda's per-invocation model fits async workers well but is a poor fit for SSE (requires keep-alive, hits invocation timeouts, awkward to scale steady connections). Holding a persistent service for the dashboard alongside Lambda workers is possible but adds two deploy targets where one architectural pattern with two components is cleaner.

### Inline classification, with a circuit breaker on Claude latency

Rejected. Circuit breakers protect downstream services from cascading failure; they don't make slow operations fast. When the breaker opens, the dashboard returns errors instead of classifications — same outage shape, dressed up.

### Skip the queue, use background tasks within the same process

Rejected. "Just spawn a goroutine / asyncio task" works at the prototype level and quietly fails at any operational seriousness: no retry semantics, no visibility, no durable record if the process crashes mid-task, no horizontal scaling. The queue is the seam that makes the worker pool scalable and the work durable.

## Consequences

- **Easier:** Dashboard latency is bounded by Postgres + Go, both of which are under our operational control. Claude's bad day cannot bring the dashboard down.
- **Easier:** Worker pool scales independently of read traffic. Backfill bursts and steady-state classification share the same pattern with different queue depths.
- **Easier:** Each component has a single clear responsibility. The Go service is "serve reads"; the Python worker is "do LLM work and write results." Failure modes are independently diagnosable.
- **Easier:** Interview narrative — "we separate latency profiles structurally; slow operations never block reads" is a senior answer.
- **Harder:** Two deploy targets instead of one. Two sets of logs to correlate when debugging an end-to-end flow. Mitigated by tracing both ends to LangSmith and structured logs in the service.
- **Harder:** A queue is operational surface. SQS is cheap and managed, but it's still a dependency to monitor (queue depth alarms, DLQ handling, retry policy).
- **Harder:** The SSE-from-worker-to-browser path requires the service to bridge the worker's output stream back to the user's connection. Concretely: worker writes intermediate state to Postgres or publishes to a pub/sub channel; service watches that channel and pushes to the SSE connection. One extra hop; well-trodden pattern.
- **Accepted commitment:** Any future feature that would naturally be "synchronous inline" must be evaluated against this decision. Easy in-process work stays in the service; anything that calls Claude, or otherwise depends on a third-party latency profile, goes through the queue.

## Deferred to future ADRs

- **Deploy platform** (App Runner / Fargate / Lambda / EC2 / EKS). Decision blocked on actually having running code and traffic shape to inform the choice. The pattern in this ADR is platform-agnostic.
- **Queue technology** (SQS / EventBridge / SNS / a self-hosted option). Default assumption is SQS for FIFO classification work plus EventBridge for scheduled triggers; locking that in deserves its own short ADR when we wire the first worker.
- **Streaming infrastructure** (SSE directly from the service vs. WebSocket vs. polling fallback). Deferred until the first interactive query feature lands and we know the actual UX needs.
