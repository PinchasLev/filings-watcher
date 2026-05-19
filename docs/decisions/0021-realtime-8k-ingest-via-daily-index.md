# 0021. Realtime 8-K ingest via EDGAR daily index

- **Status:** Accepted
- **Date:** 2026-05-19
- **Supersedes:** [ADR 0006](0006-edgar-ingestion-via-per-company-submissions-feed.md)

## Context

The product classifies every 8-K filed and surfaces results to users who choose a watchlist on the frontend, filtered at read time. The ingest path in [ADR 0006](0006-edgar-ingestion-via-per-company-submissions-feed.md) pulls per-company submissions feeds for a small fixed watchlist — `O(companies × tick)` requests, workable for a hand-chosen short list but unreachable for "all 8-Ks." EDGAR's 10 req/sec ceiling makes the per-company path impossible across the ~10,000 active CIKs filing 8-Ks.

EDGAR publishes `form.<date>.idx` for each business day, sorted by form type and updated as filings are accepted. One fetch per tick returns every 8-K filed that day. ADR 0006 anticipated this path as "Tier 1" and deferred it.

8-K volume:

- ~30K/year, ~120 average per business day.
- Clustered: 500–1,000 in earnings-season post-close windows; 20–50 off-season.
- Classification at Claude Haiku pricing: ~$0.003–0.01 per filing → ~$100–300/year all-in.

The scaling concern is not aggregate volume but bursts: a post-close avalanche of 500+ filings within an hour can saturate Anthropic's per-minute token caps even at modest concurrency.

Backfill of historical 8-Ks is out of scope. The first cursor advance is "now"; replay of historical daily-index files is an operator-on-demand capability whose merge semantics are guaranteed by the accession-number uniqueness constraint.

## Decision

The orchestrator ingests every 8-K from EDGAR's daily index, going forward from the first cursor advance. The per-company-submissions path is removed from the ingest hot path; it remains available behind the existing `fetch-edgar` operator CLI.

### Source feed

Each tick fetches `https://www.sec.gov/Archives/edgar/daily-index/<year>/QTR<n>/form.<date>.idx`, parses the pipe-delimited rows, and filters to `form_type == "8-K"`. The filing body is then fetched per [ADR 0007](0007-edgar-document-fetch-html-parsing.md) (unchanged).

Cross-day handling: a tick that runs across midnight ET fetches yesterday's index first, then today's.

### Cursor

A new singleton table holds the last successfully-persisted accession and filed-at timestamp. The cursor is advanced after each filing's classification is persisted, not after the batch. A crashed tick leaves the cursor at the last good filing; the next tick resumes there. The table lands via a forward-only additive migration per [ADR 0020](0020-secrets-and-migration-rollback.md).

```sql
CREATE TABLE ingest_cursor (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  last_accession_number TEXT NOT NULL,
  last_filed_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

`updated_at` is supplied by the application on every write (UTC ISO-8601). The schema carries no engine-specific DEFAULT clause, matching the portable-SQL convention established in migration 001 — the same path `classified_at` follows on the `classifications` table.

The cursor is a query-narrowing optimization, not the correctness mechanism. The accession-number unique constraint on the filing table rejects duplicates at insert time — a backfill replay or a re-run of the same tick produces no double-classification. Reclassification under a new taxonomy or model version follows the versioned-classifications scheme of [ADR 0011](0011-classification-history-and-reclassification.md).

### Burst handling and rate limits

EDGAR: one daily-index fetch per tick plus one body fetch per new filing. A 500-filing burst is 501 requests in a single tick. Throttled at 2 req/sec via the shared limiter from [ADR 0012](0012-ingestion-cadence-periodic-v0-push-v1.md), that's ~250 seconds of EDGAR I/O — under the 12-minute tick timeout from the same ADR.

Anthropic: the bottleneck under burst. A 500-filing tick at ~5K tokens average input each implies ~2.5M tokens, exceeding tier-2 per-minute caps. The orchestrator handles 429 and 5xx with exponential backoff (initial 1s, max 60s, jitter ±20%) up to 5 retries per filing. Filings whose retries exhaust within a tick remain for the next tick; the cursor advances only past filings whose classification persisted. A multi-tick outage produces a backlog that drains naturally as the limiter recovers.

### Host and database impact

Daily-index fetch and parse: ~1–2 MB compressed, ~120 rows on average days, ~1,000 on peak. Negligible CPU and memory.

Body fetch + classify, sequential per filing: peak memory ~150–250 MB during the LangGraph + Anthropic SDK call. Fits inside the t4g.small 2 GB host with margin. Multi-process concurrency is deferred until measured throughput demands it.

SQLite writes scale linearly with new filings per tick. WAL mode ([ADR 0008](0008-sqlite-for-v0-persistence.md)) preserves read availability for the Go service during writes. The accession-number unique index dedupes with one indexed lookup per row.

### Observability

Structured-log fields per tick, emitted to journald per [ADR 0013](0013-operational-observability-for-v0.md) with stable names for the OTel pipeline of [ADR 0018](0018-observability-otel-native-operator-controlled.md):

- `tick_started`, `tick_completed` with `duration_ms`, `new_filings_count`, `errors_count`.
- Per filing: `filing_fetched`, `classification_started`, `classification_completed` with `accession_number`, `cik`, `form_type`, `filed_at`, outcome, token usage, retry count.
- `cursor_advanced` with new accession and filed-at.
- `rate_limited` with `provider` (`edgar` or `anthropic`), retry attempt, backoff duration.
- `tick_failed` with exception class and the accession on which failure occurred.

These names are the dimensions the OTel exporter and dashboards join against. No HTTP endpoint exposes ingest state — operator visibility is `journalctl -u filings-orchestrate.service` until the OTel collector lands.

## Consequences

Coverage extends from a fixed watchlist to every 8-K as filed. Bandwidth becomes `O(filings)` instead of `O(companies × tick)` — at v0 volume this is smaller, not larger.

Item-number metadata is no longer pre-populated by the source (the daily index carries form type but not Item disclosures). The orchestrator parses Items from the filing body, which it already does for classification.

User-watchlist filtering moves entirely to the read side. A later slice adds a `?ticker=...` or `?cik=...` filter to the `/filings` endpoint; the ingest path requires no change to support it.

Failure modes:

- EDGAR daily-index unreachable: tick retries; cursor unchanged; recovery is one tick.
- Filing body unreachable: that filing's classification is skipped; cursor does not advance past it; the next tick retries.
- Anthropic exhausted: persisted classifications stay; cursor advances only past them; backlog drains over subsequent ticks.
- Orchestrator killed mid-classify: cursor reflects the last persisted classification; restart resumes there.

Operator-on-demand classification via `filings-orchestrate-once` is preserved unchanged.

## Deferred

- **Backfill replay** of historical 8-Ks via an operator CLI iterating historical `form.<date>.idx` files. The accession-number unique constraint guarantees safe merges into the live DB; backfill and live ingest coexist without coordination.
- **Expected-filings calendar** (anticipated periodic-report deadlines, earnings-call dates, missed-event alerts). Its source is outside EDGAR and its refresh model is a separate decision; covered in a future ADR.
- **Anthropic spend cap.** Process-level enforcement and AWS-billing alarms land in a separate ADR.
- **Multi-process or async concurrency** for classification. Sequential single-process is the v0 shape; revisit when measured throughput demands it.
