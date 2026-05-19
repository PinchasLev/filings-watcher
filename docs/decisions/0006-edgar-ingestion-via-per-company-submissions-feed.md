# 0006. EDGAR ingestion via per-company submissions feed

- **Status:** Superseded by [ADR 0021](0021-realtime-8k-ingest-via-daily-index.md)
- **Date:** 2026-05-13

## Context

SEC EDGAR exposes filings through several access paths. The two most relevant for an 8-K ingestion pipeline:

1. **Per-company submissions feed** — `https://data.sec.gov/submissions/CIK{cik}.json`. Returns recent filings for one company in a columnar JSON shape, including form type, filing date, accession number, primary document name, and (for 8-K) the comma-separated Item numbers disclosed.
2. **Global daily index** — `https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{n}/form.{date}.idx`. Returns all filings across all companies for a given day, indexed by form. This is the "firehose" path.

V0 has a small, fixed watchlist (tens of companies) and serves a dashboard showing recent 8-Ks for those tickers. There is no requirement to monitor every 8-K filed by every public company; that is a Tier 1 capability.

EDGAR enforces two operational constraints on either path:

- Every request must carry a descriptive `User-Agent` header including a contact email; otherwise the response is HTTP 403.
- Fair-use limit of 10 requests per second per IP. Sustained violations risk a longer block.

## Decision

The v0 ingestion path uses the **per-company submissions feed**. For each ticker on the watchlist, the worker fetches `CIK{cik}.json`, projects the columnar `filings.recent` block into row-oriented Filing objects, and filters to `form == "8-K"`.

The ticker → CIK lookup uses `https://www.sec.gov/files/company_tickers.json`. The index is small (~10k entries), updates infrequently, and is fetched on demand in v0; a real deployment caches it.

A shared `EdgarClient` enforces the User-Agent and rate-limits to 9 requests/second (one below EDGAR's stated ceiling, to leave headroom for retries).

## Alternatives considered

### Global daily index (the firehose)

Rejected for v0. The firehose returns all filings for a date, which means ingesting ~1,500 daily filings to find the ~20 the watchlist cares about — a 75× amplification of work and bandwidth for the v0 use case. The firehose is the right path for Tier 1 ("alert on any 8-K matching criteria across all public companies"), and the ingestion code will grow a second path then. For v0, the watchlist scope is bounded and the per-company feed is the matching access pattern.

### EDGAR full-text search API

Rejected. EDGAR's search endpoints (`efts.sec.gov/LATEST/search-index`) return query results, not raw filing metadata. They're useful for ad-hoc text search across the corpus — a Tier 2 capability when we want "find filings mentioning X" — but not for the structural "pull recent 8-Ks for this company" ingestion task.

### Polling RSS / Atom feeds

Rejected. EDGAR offers RSS feeds per company and per form. They contain less structured data than the JSON submissions feed (no Item numbers, looser date formats), and parsing XML adds dependency surface that JSON avoids. The JSON feed is strictly better for our use case.

### Third-party data providers (e.g., paid aggregators)

Rejected. Direct EDGAR access is free, has no licensing constraints on derivative use, and avoids vendor lock-in. The cost of building the client is bounded; the cost of a paid provider would compound and would introduce a single point of failure outside our control.

## Consequences

- **Easier:** Direct, structured access to per-company filings. The columnar JSON format is straightforward to project into typed objects. Item numbers — the natural classification taxonomy — arrive in the metadata feed without needing to fetch the full filing.
- **Easier:** Bounded request volume scales linearly with watchlist size, not corpus size. A 20-company watchlist hits two endpoints (ticker index + 20 submission feeds) per refresh cycle, well under the rate-limit ceiling.
- **Harder:** Watchlist scope is a hard constraint of the v0 architecture. Supporting "all material 8-Ks across the market" requires adding the daily-index path as a parallel ingestion source. The Filing model and storage will accommodate either source; the client and fetch logic gain a second mode.
- **Accepted commitment:** The `EdgarClient` enforces operational requirements (User-Agent, rate limit) at the HTTP layer. Bypassing the client to make direct requests would risk getting blocked by EDGAR; future code paths must use the client.

## Deferred

- **Caching the ticker index.** V0 fetches `company_tickers.json` on each lookup. A simple file-based cache with a daily refresh is sufficient and will land when the ticker → CIK call becomes a hot path.
- **Document body fetching.** This ADR covers the metadata feed only. Fetching and parsing the actual 8-K document (HTML/HTM, occasionally PDF) is a separate concern: it requires HTML parsing, handles much larger payloads per filing, and is the input to the classifier — covered in a follow-up ADR alongside the classifier.
- **Async/concurrent ingestion.** The v0 client is synchronous. When the worker pool needs to ingest many tickers concurrently, either `httpx.AsyncClient` or a thread pool will lift throughput; the rate-limiter design (one global token bucket) already accommodates both.
