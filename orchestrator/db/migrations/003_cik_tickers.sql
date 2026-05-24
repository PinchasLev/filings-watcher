-- 003_cik_tickers
--
-- Creates the cik_tickers table: the local mirror of SEC's authoritative
-- CIK→ticker mapping (https://www.sec.gov/files/company_tickers.json).
--
-- CIK is the stable identifier for a SEC registrant — the same legal
-- entity keeps its CIK through ticker changes, rebrandings, and most
-- reorganizations. Filings are anchored on CIK (derived from the
-- accession number); this table provides the join from the user-facing
-- ticker to that CIK. See ADR 0025 for the rationale and tradeoffs,
-- including the explicit deferral of historical ticker-at-filing-time
-- tracking (current-state is sufficient for v0's short corpus).
--
-- The cik column is the zero-padded 10-digit form (matching the
-- format used by the filings table). The SEC source publishes CIK
-- as an integer; the scan-tickers CLI zero-pads on ingest.

CREATE TABLE cik_tickers (
    cik          TEXT PRIMARY KEY,
    ticker       TEXT NOT NULL,
    company_name TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Reverse lookup: ticker → CIK. Frequent on the "?ticker=AAPL" filter
-- path, so an index keeps it cheap.
CREATE INDEX cik_tickers_ticker_idx ON cik_tickers(ticker);
