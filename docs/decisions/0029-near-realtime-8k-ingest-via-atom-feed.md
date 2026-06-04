# 0029. Near-real-time 8-K ingest via the EDGAR Atom feed

- **Status:** Proposed
- **Date:** 2026-06-03

## Context

[ADR 0021](0021-realtime-8k-ingest-via-daily-index.md) ships ingest against EDGAR's daily-index file. That file is published once per day, around 10 PM ET, so the freshness floor is bounded by the publication cadence: a filing made at 09:30 ET appears in our feed roughly 12.5 hours later. [ADR 0012](0012-ingestion-cadence-periodic-v0-push-v1.md) anticipates a push-driven evolution but leaves its concrete shape unresolved.

The product context has shifted enough to force the choice. A concrete user — a day trader on the live site — requested visibility on filings within minutes of submission, not hours after the close. Any editorial work that follows from the events layer ([ADR 0027](0027-two-pass-classification-filing-level-events.md), [ADR 0028](0028-runs-as-versioning-axis-and-reprocessing.md)) — significance scoring, sector anomaly detection, signal generation — has bounded value below this latency floor. An editorial signal that lands hours after the event has narrow utility for the use cases it is meant to inform.

EDGAR offers `https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom&count=N`, a synchronously-pulled Atom snapshot of the most recent N 8-K submissions (default 40, expandable). Entries appear within minutes of an issuer's submission. The endpoint is not a push primitive; it is a snapshot you poll. Latency is bounded by the poll interval, not by listener uptime, and miss-risk is bounded by the rate at which entries roll off the tail of the snapshot — at observed 8-K volume, comfortably outside the polling cadence proposed here.

[ADR 0006](0006-edgar-ingestion-via-per-company-submissions-feed.md) (superseded) rejected RSS/Atom partly because the feed lacked Item numbers and used looser date formats than the per-company JSON. That objection is no longer load-bearing: the pipeline now extracts Items from the filing body, not the feed metadata, and the Atom feed emits ISO-8601-compatible timestamps. The other half of ADR 0006 — the per-company submissions feed — was superseded by ADR 0021 in favor of firehose coverage; that decision stands.

## Decision

A second ingest CLI, `scan-atom-feed`, becomes the primary near-real-time ingest path. The existing `scan-daily-index` is retained at a slower cadence as a reconciliation backstop. Both run as the existing Python orchestrator process under separate `systemd` timers; the read-side Go service remains read-only.

### Primary path — Atom polling

- A new entry-point CLI polls `https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom&count=100` on a 30-second timer. The 30-second value is a starting tunable, declared in runtime configuration per ADR 0012's tunables-in-config rule. The timer uses `OnUnitInactiveSec`, not `OnCalendar`, so the next tick fires 30 seconds after the previous tick *completes* — overlap is impossible by construction. `flock -n` on `ExecStart` defends against manual-invocation overlap; `TimeoutStartSec` bounds a stuck tick. These are the same discipline ADR 0012 applies to the existing v0 tick, inherited unchanged.
- Each entry is parsed and dedup'd against the `filings` accession-number primary key (`INSERT OR IGNORE`, no schema change). New entries flow through the existing per-filing pipeline unchanged: `fetch_filing_document` → `classify_filing` (with retries per ADR 0021) → `reduce_filing` (best-effort per ADR 0027 / ADR 0028) → persist.
- No cursor is introduced. Idempotency on the accession PK is sufficient; the Atom snapshot is forward-only in practice, and dedup is the durability mechanism.

### Backstop — daily-index reconciliation

- `scan-daily-index` runs on its own timer as a clustered series of late-evening invocations scheduled via `OnCalendar` — 10:15, 10:30, 10:45, and 11:00 PM ET — to catch EDGAR's once-daily publication regardless of routine slippage within that window. The current 15-minute round-the-clock cadence is retired; the daily-index file does not exist intraday, so high-frequency polling against an absent endpoint serves no purpose. Idempotency on the accession PK makes the redundant runs free: whichever invocation finds the published file ingests, and subsequent invocations dedup-and-exit without processing.
- If the 11:00 PM ET invocation finds no published file for the current date, it emits a structured `daily_index_publication_missing` event carrying the date and a derived `is_business_day` flag. Weekend dates are expected and the event is informational; business-day misses route to alarm. The event joins `cost_observed` and `tick_failed` on the same observability surface seeded by the spend cap. If publication slips past 11:00 PM ET (rare), the next evening's cluster catches the miss on the following day.
- When an invocation first detects a newly-published file for the current date, it emits a structured `daily_index_published` event carrying the detection timestamp, the date, the total entry count, and the 8-K subset count. The event is informational rather than alarm-routed; it provides the normal-state baseline against which `daily_index_publication_missing` events become legible as anomalies rather than noise.
- The backstop processes any filing the Atom path missed (parser error, network blip, EDGAR transient, process down). At steady state it processes zero new filings; its value is bounded and proportional to the rarity of misses, which the observability events above are expected to confirm is low.

### Spend cap

ADR 0012 names the Anthropic spend cap as a load-bearing commitment for any unattended run. Under the daily-index cadence, a runaway exhausts credit and surfaces as a single nightly failure. Under near-real-time, a runaway exhausts credit and continues to fail every 30 seconds against the cap. The latency reduction makes the cap deploy-gating, not eventual. This ADR scopes a minimum mechanism that ships with the Atom path:

- Each Anthropic call emits a structured `cost_observed` event carrying model name, input and output token counts, and an estimated cost computed against published per-token pricing.
- A daily aggregate is queryable from the events log.
- Each tick begins by reading the aggregate against an operator-configured cap. Above the cap, the tick logs `tick_failed` with `error_class=cost_cap_exceeded` and exits non-zero. Above an operator-configured warning threshold, an alarm event fires.
- The cap and warning threshold are runtime configuration; defaults are set conservatively against the prior month's average daily spend.

The spend cap is the seed of the broader observability surface deferred by ADR 0012 and tracked in [ADR 0013](0013-operational-observability-for-v0.md). Subsequent observability concerns — per-tick processing-time visibility, an ingest-vs-processing equivalent of queue depth, alarm thresholds — extend the same event-shaped surface incrementally rather than each adding a one-off mechanism.

### Deferred — watcher + worker architecture

The long-running watcher + worker pool from ADR 0012's v1 sketch — dispatching one Python worker per filing by SQLite-queue claim, by `os.fork()`, or by per-invocation Lambda — remains the documented architecture for scaling beyond single-process throughput. It is correct for that problem; the problem is not present today. Promotion criteria, made concrete by the observability surface seeded above:

1. Sustained per-tick processing time exceeding the tick interval, observed over an earnings-day window.
2. An ingest-vs-processing backlog (filings appearing in the Atom snapshot but not yet processed) exceeding an operator-configured threshold for sustained intervals.
3. Operator-judged risk to freshness during peak earnings season after at least one observed season.

None of these are present today. Promotion is additive: the watcher and workers extend the existing pipeline, they do not rewrite it.

## Rationale

### Why Atom rather than the daily-index polled faster

The daily-index file for date D is published once per day around 10 PM ET. Polling it more frequently does not reduce freshness — the data does not exist intraday. The latency floor of the daily-index path is the publication cadence, not the polling cadence. Real-time freshness requires an endpoint whose backing data is published intraday; the Atom feed is that endpoint.

### Why single-process polling rather than a watcher + worker pool

ADR 0012's v1 watcher + worker pool is the right architecture for scaling beyond single-process throughput. At observed volume — peak earnings days produce hundreds of 8-Ks spread over hours, not concurrent floods — a single Python process polling every 30 seconds and processing serially completes within freshness budgets with substantial margin. A burst of 50 filings at ~10 seconds of per-filing processing drains in roughly eight minutes, well inside the "minutes of filing" target.

Promoting to the watcher + worker pool now solves a problem we do not have, at the cost of new process boundaries, new infrastructure, and new failure modes (stranded claim rows, worker-pool sizing, cross-process rate-limiter coordination). The pool architecture is preserved above as a deferred promotion path with concrete criteria; building it when the observability surface flags saturation is correctly-timed.

### Why keep the daily-index path as a backstop

ADR 0012 originally proposed retiring the v0 timer in a single PR. The Atom path's pull-snapshot semantics create a small but real miss floor: an outage, a parser regression, an EDGAR transient that returns a malformed feed for one poll cycle. None catastrophic; none worth dropping a filing over.

The daily-index file is, by EDGAR's contract, complete for date D as of publication. Polling it once after publication produces a definitive reconciliation: every accession not yet in `filings` is one Atom missed. Idempotency on the accession PK makes the backstop free in the common case — dedup short-circuits before any pipeline work — and decisive in the rare case. The existing code already implements this path correctly; retiring it would be a deletion in service of nothing.

### Why surface backstop slippage rather than add more backstop runs

A "final" run at 11:30 PM or midnight has the same uncertainty as the 11:00 PM run: if it misses, the operator wants another, and the chain has no natural terminus. EDGAR's publication time has no SLA, and any client-side schedule is a guess; building more guesses adds noise and code without bounding the failure mode. Surfacing the condition through observability bounds it differently — the operator distinguishes a single delayed publication (no action; the next evening's cluster reconciles) from a developing pattern (worth investigating) using a signal the system already needs for other reasons. The Atom path's near-real-time coverage makes the backstop's job anomaly detection more than ingest; the right surface for anomalies is observability, not more backstops.

### Why the spend cap is in scope here

ADR 0012 named the cap load-bearing, but its implementation was implicitly bounded by the once-daily tick: the worst-case runaway processed a finite backlog and stopped. The Atom path fires the classifier and the reducer throughout the day. A runaway under near-real-time does not announce itself as a single nightly failure; it announces itself as a credit exhaustion mid-day, silently breaking ingest until an operator notices. Deploying the Atom path without the cap is taking on a known risk the original ADR explicitly named as unacceptable.

The cap as the seed of an incremental observability surface is also right-sized for this slice: small enough to ship with the Atom path, large enough that the event shape, the aggregation pattern, and the alarm conventions transfer to the next observability concern without rework.

### Why Atom's lack of metadata richness no longer matters

ADR 0006 rejected RSS/Atom partly because the per-company JSON exposed Item numbers directly while the Atom feed required parsing the filing body for them. The current pipeline parses Items from the body in `fetch_filing_document` regardless of the ingest path; Atom's metadata sufficiency is no longer a discriminator. The Atom feed's timestamps are ISO 8601 with UTC offsets, comparable in rigor to the JSON feed's.

## Alternatives considered

### Long-poll, SSE, or another streaming primitive

Rejected: EDGAR exposes no such primitive. Both the daily-index and Atom endpoints are pull-only HTTP. Any "streaming" framing on either is a client-side polling loop.

### Watcher + worker pool as the immediate v1

Deferred per the rationale above. The architecture is right for the scaling problem; the scaling problem is not present at observed volume.

### Watcher + AWS Lambda per filing

Deferred to a further-future evolution past the watcher + pool. Forces migration off SQLite (Lambda cannot share an EC2-resident SQLite file safely), introduces a separate deploy target with packaging and IAM surface, and adds cold-start latency proportional to LangChain's import cost. Each is solvable; collectively they are well beyond what the current latency requirement asks for.

### Higher-frequency polling of the daily-index

Rejected. Daily-index data does not exist intraday; the latency floor is publication cadence, not polling cadence. Higher-frequency polling against an absent file is wasted requests and noise in the operational log.

### Third-party EDGAR aggregator with push delivery

Rejected as in ADR 0012: paid dependency, second source of truth, and an integration surface for a property the Atom path achieves directly against the authoritative source.

### Per-company JSON submissions feed at high frequency

Rejected. ADR 0021 already supersedes ADR 0006 on coverage grounds — a small fixed watchlist is no longer the operating scope. Restoring a watchlist concept to enable per-company polling reverses an architectural decision made for sound reasons.

## Consequences

- **Easier:** Freshness floor drops from ~24 hours to under a minute at steady state, with the deviation visible in the per-tick interval rather than in publication delay.
- **Easier:** No new binary, no new process model, no migration. The change is additive — a second CLI and a second timer share the existing pipeline.
- **Easier:** The daily-index path becomes the reconciliation layer naturally; its existing tests, retries, and event surface stay valuable.
- **Easier:** The watcher + worker pool architecture remains a documented low-risk promotion path. Adding it later does not require rewriting the Atom path.
- **Easier:** The spend cap and its observability surface ship with this work, not deferred to a separate effort.
- **Harder:** Tick volume rises by roughly 30× (one per 30 seconds rather than one per 15 minutes). Per-tick cost must be near-zero — the Atom poll is one HTTP GET and a small XML parse — and per-tick logging must be quiet enough that `journald` is not flooded.
- **Harder:** A timer drift or stuck tick is now a freshness incident on a one-minute clock rather than a 15-minute clock. The `OnUnitInactiveSec` + `flock` + `TimeoutStartSec` discipline from ADR 0012 applies unchanged; threshold values may need adjustment.
- **Accepted commitment:** Anthropic spend cap is deploy-gating for the Atom path. The mechanism — structured `cost_observed` events, daily aggregate, pre-tick threshold check, alarm — ships with this slice.
- **Accepted commitment:** The Atom path shares the EDGAR rate limiter with all other EDGAR fetches. The shared limiter from ADR 0012 enforces well under the 10 req/sec ceiling regardless of poll cadence.
- **Accepted commitment:** No watchlist concept is reintroduced. The Atom feed is firehose-scoped, matching the coverage decision of ADR 0021.

## Deferred

- **Watcher + Python worker pool, fork-per-filing, or Lambda-per-filing.** ADR 0012's v1 architecture. Documented above as the promotion path with concrete criteria. Lands as a separate ADR when the observability surface shows the criteria met.
- **Broader observability beyond spend.** Per-tick processing-time metrics, an ingest-vs-processing backlog metric, alarm thresholds beyond the spend cap. Each is additive to the surface seeded here; ADR 0013 governs the surface as a whole.
- **Anthropic tier upgrade.** Revisit when the daily aggregate from this slice's observability surface shows sustained trend pressure against the cap.
- **Migration off SQLite.** Anticipated by [ADR 0008](0008-sqlite-for-v0-persistence.md), unchanged here. Not forced by the Atom path or by the deferred watcher + worker pool; would be forced by a future move to Lambda-per-filing.
