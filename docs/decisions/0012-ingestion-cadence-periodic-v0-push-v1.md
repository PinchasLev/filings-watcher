# 0012. Ingestion cadence — periodic batch for v0, push-driven for v1

- **Status:** Accepted
- **Date:** 2026-05-15

## Context

The system continuously ingests 8-K filings from EDGAR, classifies them via Claude, and serves the results. Two properties of the input shape the pipeline:

**EDGAR offers no push primitive.** There are no webhooks, no streaming feeds. "Real-time" ingestion is always polling against the per-company submissions feed or the full-text search index. The architectural question is not push vs. poll — it is *how fast* to poll, and *who absorbs* the latency cost between polls.

**8-K filing volume is highly non-uniform.** Filings cluster around pre-market (06:30–09:30 ET) and post-close (16:00–18:00 ET) windows, with a sharp spike before EDGAR's 17:30 ET filing-date cutoff. Seasonally, four annual earnings seasons produce evenings with hundreds of 8-Ks in the post-close window, against off-season days with single-digit volume. The pipeline cannot be designed for the mean; it has to absorb bursts without violating external limits.

The external limits are real and immediate: EDGAR's fair-access policy caps aggregate request rate at 10 req/sec with a required User-Agent. The Anthropic API enforces per-tier RPM and TPM limits that bursty classification can saturate. Both must be honored by every component that issues a relevant request.

v0 ships only the read-mostly dashboard ([ADR 0005](0005-compute-architecture-read-mostly-and-async-workers.md)) backed by single-host SQLite ([ADR 0008](0008-sqlite-for-v0-persistence.md)); a single Python orchestrator handles fetch + classify + write today.

## Decision

Staged pipeline. The shape evolves; the storage and rate-limit obligations are stable across stages.

**v0 — periodic batch.** The orchestrator runs as a `systemd` `oneshot` service triggered by a `systemd.timer`. Each tick: pull EDGAR submissions newer than the persisted cursor, fetch and classify each new 8-K serially (or with a small bounded internal concurrency), write to SQLite, advance the cursor, exit. Idempotency is keyed on the accession number — a tick that crashes mid-run is safe to retry on the next tick. Worst-case ingestion latency is bounded by the tick interval (initial: 15 minutes).

**Overlap and timeout discipline:**

- The timer uses `OnUnitInactiveSec=15min`, not `OnCalendar`. The next tick fires 15 minutes after the previous tick *completes*, never wall-clock-15-minutes after it *starts*. This makes overlap impossible by construction; a tick that runs 14 minutes simply pushes the next tick back. This is preferable to `OnCalendar` + `flock`, which absorbs overlap by silently dropping ticks.
- A `flock -n` wrapper on `ExecStart` defends against the residual overlap path — manual invocation (`systemctl start`) while a timer-fired run is in progress.
- `TimeoutStartSec=12m` bounds a stuck run. `systemd` kills the process, marks the unit failed, releases the `flock`, and the next tick fires after `OnUnitInactiveSec` elapses against the failure timestamp. The cap is independent of the timer interval — a longer cap simply pushes the next tick further out — and is a starting tunable, not an invariant of the schedule. Failures are observable via `systemctl is-failed` and `journalctl -u`. The structural argument for *having* a cap at all is captured below in "Why bound each tick rather than run-to-completion."

**v1 — push-driven split.** A long-running watcher (Go) polls EDGAR's submissions index every few seconds, deduplicates against a `pending_filings` table in the same SQLite database, and enqueues new accession numbers. A bounded pool of Python workers (initial size 4–8, tunable) tails the queue, fetches the filing body, classifies via Claude, and writes the result. Queue depth (`COUNT(*) WHERE status='pending'`) becomes the system's first-class saturation signal: it should spike during the post-close window and drain before the next burst. Stranded `processing` rows are reclaimed by a sweeper after a threshold.

**All operational tunables live in declarative configuration external to source code.** Two layers:

- **Scheduler-level values** (timer interval, run timeout) live in `systemd` unit files (`.timer`, `.service`). Changing the v0 tick from 15 min to 10 min is `systemctl edit orchestrator.timer` followed by `systemctl daemon-reload` — no code change, no rebuild, no redeploy.
- **Application-level values** (per-tick classification concurrency in v0; worker pool size, watcher poll interval, rate-limiter budgets, sweeper threshold, dedup window in v1) live in a runtime configuration file consumed by the orchestrator at process start. Initial defaults (15 min tick, 12 min timeout, pool size 4–8) are starting points to be tuned against observed behavior, not invariants of the design.

No operational tunable is a hardcoded constant in source. This is enforced by code review on every PR touching the pipeline.

Both stages share two infrastructure obligations:

- **Shared rate limiters.** A token-bucket limiter for EDGAR (well under 10 req/sec aggregate across all processes) and one for Anthropic (tied to current tier RPM/TPM). The watcher and worker both consult these. In v0 the limiter lives in-process; in v1 it is a small shared state (SQLite row, or process-local with conservative budgets).
- **Idempotency on accession number.** Every write keyed on `(accession_number)` with `INSERT OR IGNORE` semantics; reclassification follows the versioned-classifications scheme from [ADR 0011](0011-classification-history-and-reclassification.md).

## Rationale

### Why staged

v0's value is end-to-end correctness on a live data feed, not low latency. A 15-minute floor on freshness is honest with that goal and lets the entire pipeline ship as a single process. v1 is where the architecture earns its keep — separating polling from classification means polling cadence and classifier parallelism become independently tunable, and the queue provides the natural seam for backpressure, retry, and observability. Building v1's split before v0 has run is premature; building v1's split later requires no change to the storage layer or the read service.

### Why config-externalize the tunables

The initial values for tick interval, run timeout, and worker pool size are educated starting guesses — there is no a priori correct answer for any of them. The right values will emerge from operating the system through at least one earnings season: a tick interval that's wasteful off-season may be too slow during peak, a timeout that's generous now may need tightening once classifier latency stabilizes, a pool size of 4 may turn out to be 6 or 12. Treating these as configuration rather than code means tuning is a ten-second operation against a running system, not a code-review-build-deploy cycle. Hardcoding them would make the inevitable tuning either friction-heavy or invisible (silently drifting from the values implied by the ADR).

Config-externalization without operational visibility is performative — tuning a knob requires seeing what it's doing. The tunables in this ADR depend on the observability commitments in [ADR 0013](0013-operational-observability-for-v0.md); a deploy that lands one without the other is incomplete.

### Why bound each tick rather than run-to-completion

A run-to-completion alternative — no `TimeoutStartSec`, the orchestrator runs until the backlog is fully drained on each invocation — is simpler in steady state and finishes burst days in one continuous run. The bounded-tick approach is chosen for four reasons:

1. **Hung-process recovery.** Without a cap, a stuck Anthropic socket, deadlocked SQLite write, DNS timeout that never returns, or runaway retry loop holds the `flock` indefinitely. The timer continues firing futile ticks that immediately exit on `--conflict-exit-code=0`, and the system goes silently dead until an operator intervenes. `TimeoutStartSec` makes recovery automatic: SIGTERM at the bound, flock releases, next tick proceeds. This is the rationale ADR 0012's original timeout bullet named; the other three reasons below are corollaries.
2. **Resource footprint per invocation.** A multi-hour Python process accumulates risk in held HTTP connections, file descriptors, parsed-index buffers, and any latent memory leak in upstream libraries. Periodic forced restart is the boring, well-known cure — the same reason long-running services often carry rolling-restart policies even when nothing is observably wrong.
3. **Bounded staleness of deployed code.** A new release or configuration fix takes effect at the next tick. With a cap, the worst-case delay before exercise is bounded by `TimeoutStartSec + OnUnitInactiveSec` (currently 12 + 15 = 27 minutes). Without a cap, an active burst-day run could pin the previous binary in memory for hours.
4. **No work loss because the cursor is the durability mechanism.** Every per-filing `cursor_advanced` event is a committed SQLite write before any SIGTERM. The cap costs *latency* on burst days — work spreads across two or three ticks — but never *correctness*. The cursor design from [ADR 0021](0021-realtime-8k-ingest-via-daily-index.md) was structured for exactly this: forced restarts are recoverable, not destructive.

The 12-minute value itself is a starting tunable, not a derived quantity. The right number is whatever makes hangs recoverable within an acceptable operator-response window without artificially fragmenting normal-day work. It will be revised against operating data — first earnings-season burst, observed Anthropic latency distribution, observed daily-index publish drift — rather than against a model.

### Why systemd's `OnUnitInactiveSec`, not external scheduling

The overlap-prevention problem has a built-in answer in the chosen scheduler. `OnUnitInactiveSec` schedules the next tick relative to the previous tick's *completion*, eliminating overlap by definition and providing implicit backpressure (slow ticks naturally push subsequent ticks back). Layering `flock` on top is defense-in-depth, not the primary mechanism. Reaching for an external scheduler (EventBridge, cron) here would solve a problem the local scheduler already solved.

### Why bounded parallelism, not fan-out

The peak post-close burst on a heavy earnings day is hundreds of 8-Ks. Naive fan-out — one classification call per new filing — would saturate the Anthropic API rate limit, exhaust EDGAR's 10 req/sec budget across the watcher's polling and the worker's fetches, and produce a thundering-herd write pattern against SQLite's single-writer lock. A bounded pool with shared rate limiters absorbs the same burst at a deterministic, sustainable throughput, with the queue as the buffer.

## Alternatives considered

### Long-running orchestrator daemon from v0

Rejected for v0. Functionally equivalent to the timer-driven approach (same code, same dependencies, same external API surface) but adds internal-loop state, signal-handling, and a "is the process still alive and making progress" question that the timer-driven design answers for free. v1 introduces the long-running watcher when the architecture demands it — at that point, the additional complexity is paying for continuous polling rather than for nothing.

### `OnCalendar` wall-clock timer with `flock` mutex

Rejected. Functionally workable but silently drops ticks when the previous run runs long: the wall-clock tick fires, `flock` rejects it, and the cadence is silently violated. `OnUnitInactiveSec` makes the same backpressure explicit in the scheduling semantics and produces a regular, predictable trace.

### EventBridge → Lambda trigger

Rejected. Introduces a second deploy target, IAM role surface, and packaging discipline, in exchange for a feature (managed cron) that `systemd` already provides on the host the service runs on. Reasonable in a multi-host or serverless deploy topology; premature on the single-host v0 substrate.

### Unbounded worker fan-out in v1

Rejected. Violates both the Anthropic API rate limit and EDGAR's fair-access policy during bursts. The remediation is the same bounded-pool design adopted here; adopting it from the start avoids a migration after the first incident.

### External queue (Redis, SQS) for v1

Deferred, not rejected on merit. SQLite-as-queue is sufficient for single-host v1 and adds no infrastructure: the queue lives in the same database the workers already read and write, observability is `SELECT` queries, durability is the existing backup story. The external-queue decision is the right one to revisit when v1 outgrows a single host (cross-host workers, fan-out across regions); that's a future ADR.

### Third-party EDGAR aggregator with push delivery

Rejected. Paid dependency, second source of truth (their normalization may diverge from raw EDGAR), and an extra integration surface for a property — sub-minute filing notification — that v1's continuous polling achieves directly against the authoritative source.

## Consequences

- **Easier:** v0's latency story is one number — the tick interval — and it is honest. No claims of sub-minute freshness while a serial classifier holds the line behind a 15-minute timer.
- **Easier:** The v0 → v1 transition does not touch storage, the read service, or the classifier itself. The watcher and worker pool are additive; the timer-driven orchestrator can be retired in a single PR.
- **Easier:** Burst behavior is visible in one query (`COUNT(*) WHERE status='pending'`) once v1 lands. Saturation, backlog, and drain rate become observable without dedicated metrics infrastructure.
- **Easier:** Tuning is an operational activity, not a development one. The 15-min tick, 12-min timeout, and worker pool size are starting values, not commitments; changing them does not touch source code, run CI, or require a deploy.
- **Harder:** v1 introduces producer/consumer concerns the v0 single-process model avoids: stranded `processing` rows after worker crash, queue-depth alarming, rate-limit coordination across two processes, and worker-pool sizing as an operational tuning surface.
- **Harder:** SQLite's single-writer lock serializes worker writes. At v1's expected volume this is invisible; at any volume that requires concurrent writers, it becomes a bottleneck — and the answer there is migration off SQLite ([ADR 0008](0008-sqlite-for-v0-persistence.md) anticipates this), not a structural change to this ADR.
- **Accepted commitment:** Anthropic API spend must have a configured cap before any unattended run. A bug that loops a classifier on the same filing is unbounded cost without the cap. This is operational discipline, not architecture, but it is load-bearing.
- **Accepted commitment:** EDGAR's User-Agent and 10 req/sec policy is a contract, not a guideline. Every component issuing EDGAR requests goes through the shared rate limiter.
- **Accepted commitment:** Operational tunables stay in configuration. Any PR that introduces a hardcoded constant for a value an operator would reasonably want to change at runtime is rejected on review.

## Deferred

- **[ADR 0013](0013-operational-observability-for-v0.md) — Operational observability for v0.** The minimum surface required for the tuning loop this ADR commits to: structured `systemd-journald` logging, a `runs` table capturing per-tick outcomes, LangSmith capture of Claude calls, and a unit-failure alarm. Lands alongside the Phase 4 deploy, not after.
- **The v1 watcher's concrete shape** (single binary, polling interval, dedup window, queue schema). Decision blocked on v0 operating long enough to observe real burst behavior and choose interval/concurrency numbers from data rather than guesses.
- **Migration off SQLite** when concurrent-writer pressure or cross-host worker placement forces it. Anticipated, not scheduled.
- **Anthropic API tier selection and spend caps.** Operational decision tracked outside ADRs; revisit when classifier volume justifies a tier upgrade.
