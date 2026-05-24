# 0025. Ticker enrichment: CIK as the stable join key, current-state ticker mirror

- **Status:** Accepted
- **Date:** 2026-05-24

## Context

The orchestrator persists filings with their CIK (derived from the accession number) but has been writing `ticker` as `NULL` because the daily-index source EDGAR exposes doesn't carry the ticker symbol. Users browsing the product expect to navigate by ticker — "show me filings for AAPL" is a more natural query than "show me filings for CIK 0000320193."

The remediation has two layers:

1. **A source of CIK → ticker.** SEC publishes the authoritative mapping at <https://www.sec.gov/files/company_tickers.json> — a flat JSON object, ~10,000 entries, updated as companies IPO, delist, or change tickers. Free, deterministic, no auth.
2. **A semantic decision about which identifier is the join key.** Ticker is mutable (rebrands, exchange listing changes, dual-class shares, mergers). CIK is the stable legal-entity ID SEC assigns once and rarely revokes. Anchoring on the mutable label leaks instability into every downstream feature; anchoring on the stable identifier and treating the label as presentation does not.

## Decision

`cik` is the join key for everything. `ticker` is a mutable presentation attribute, mirrored from SEC's published mapping and refreshed periodically.

Concretely:

- **New `cik_tickers` table** (migration 003) holds the SEC mapping locally — one row per CIK, with `ticker`, `company_name`, and `updated_at`. The cik column is the zero-padded 10-digit form matching `filings.cik`.
- **New `scan-tickers` CLI** fetches the SEC JSON, normalizes the integer CIKs to zero-padded strings, and upserts into `cik_tickers`. Idempotent.
- **At classify time**, `scan_daily_index.py:_process_one` looks up the ticker by CIK from `cik_tickers` and copies it onto the `filings.ticker` column before persisting. New filings receive the ticker without needing a separate backfill pass.
- **Backfill** runs at the end of `scan-tickers`: `UPDATE filings SET ticker = ... WHERE ticker IS NULL AND cik IS in cik_tickers`. One-shot SQL; idempotent.
- **Joins for ticker-based queries** go through CIK: `?ticker=AAPL` resolves to a CIK via `cik_tickers`, then queries `filings WHERE cik = ?`. Filings made when the company traded under a different ticker — but the same CIK — naturally surface.

## Alternatives considered

### LLM-based ticker extraction

Rejected. The mapping is deterministic and authoritative at SEC. An LLM call would be slower, costly per filing, and could hallucinate plausible-but-wrong tickers. The right tool for a closed-form lookup is a lookup, not a language model. (The `[[tool-fit-over-familiarity]]` principle, restated for AI tools: don't reach for an LLM when the canonical data exists.)

### Per-render lookup from SEC

Rejected. Fetching the SEC JSON on every page render would add ~MB of bandwidth per request and depend on SEC's availability during user traffic. The local mirror in `cik_tickers` decouples user latency from SEC's response time and works in offline / degraded modes.

### A third-party ticker API (Polygon, IEX Cloud, etc.)

Rejected. SEC is authoritative for SEC-registered identifiers. Third parties derive from SEC anyway; using them adds a paid dependency, a rate-limit surface, and an additional ToS to manage — for data SEC publishes free.

### Maintain ticker history at filing time (immutable snapshot)

Deferred. For v0's short corpus (filings from the past few weeks), ticker changes within the corpus window are rare-to-nonexistent, so showing the *current* ticker on every filing is correct and useful. When the corpus grows to multi-year history, ticker-as-of-filing-time becomes a meaningful distinction (Facebook → Meta, Square → Block, etc.) and we'd capture an immutable ticker snapshot on the `filings.ticker` column at classification time, refreshing only when the row is rewritten. The decision to defer is reversible without schema change — the column already exists; the policy of "current-state mirror" vs. "immutable snapshot" is a write-time choice.

### Schedule `scan-tickers` via systemd timer in this PR

Deferred to a separate operational follow-up. SEC's mapping updates slowly (companies IPO/delist on order of days, not minutes); manual `scan-tickers` invocations during deploys are sufficient until the cadence becomes real friction.

## Consequences

- **Easier:** ticker enrichment is a deterministic ingest with no LLM dependency, no rate limit, no recurring cost. Schema is one table; CLI is ~80 lines; backfill is one SQL statement.
- **Easier:** the `?ticker=AAPL` UI filter naturally surfaces filings made under previous tickers (Facebook era of Meta), because the join goes through CIK. Searching by *current* ticker returns the company's *whole* filing history regardless of what ticker each individual filing was made under.
- **Easier:** the editorial-agent and discrepancy-detection features (future) can presume a stable join key for matching counterparties and cross-referencing. Building those against ticker would have inherited ticker's instability.
- **Harder:** searching by a historical ticker (e.g., `?ticker=FB` today) returns nothing, because FB no longer maps to any CIK in current `cik_tickers`. The historical ticker → CIK resolution would require a ticker-history table (`cik_ticker_history`) capturing every (cik, ticker, start_date, end_date) tuple. Deferred; not blocking v0.
- **Accepted commitment:** `cik_tickers` is a current-state mirror, not a historical archive. We re-run `scan-tickers` to refresh; old ticker rows are overwritten on conflict. The mutation is intentional.
- **Accepted commitment:** filings displayed in the UI show the *current* ticker of the issuing CIK, regardless of when the filing was made. Historical-ticker UX is deferred to a future enrichment.
- **Accepted commitment:** when a private subsidiary, trust, or non-trading registrant files an 8-K, `cik_tickers` has no entry; `filings.ticker` remains NULL; the UI shows "no ticker" gracefully. Roughly ~20-30% of registrants in any given daily batch fall into this category.
