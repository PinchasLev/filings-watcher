-- 015: periodic-filing ingest — periodic_filings envelope, filing_blocks, cursor.
--
-- Portable SQL — must compile and run identically on SQLite and Postgres. The
-- application supplies all timestamps (UTC ISO-8601); no engine-specific defaults.
--
-- Change-detection (ADR 0042) ingests 10-K filings, segments Item 1A (Risk
-- Factors) into whole risk-factor blocks, and stores them for period-over-period
-- diffing.
--
-- periodic_filings is the dedup ANCHOR and completeness ledger — one row per
-- PROCESSED 10-K, written even when the document yields no blocks (non-markup,
-- oversized, or no locatable section), so dedup keys off THIS table, mirroring the
-- Form-4 insider_filings envelope (ADR 0038). period_of_report is the fiscal
-- period end (auditable) and the pairing key the diff uses to find the prior
-- year's filing; fiscal_year is its calendar year, a display/grouping convenience.
-- parsed=0 marks a filing we fetched but could not segment (non-markup/oversized),
-- so it stays anchored and is not re-fetched.
CREATE TABLE periodic_filings (
    accession_number   TEXT PRIMARY KEY,
    cik                TEXT NOT NULL,
    company_name       TEXT,
    form               TEXT NOT NULL,
    filed_at           TEXT NOT NULL,
    period_of_report   TEXT,
    fiscal_year        INTEGER,
    parsed             INTEGER NOT NULL DEFAULT 1,
    block_count        INTEGER NOT NULL DEFAULT 0,
    ingested_at        TEXT NOT NULL
);

CREATE INDEX idx_periodic_filings_cik_period ON periodic_filings (cik, period_of_report);
CREATE INDEX idx_periodic_filings_filed_at ON periodic_filings (filed_at);

-- filing_blocks: the segmented risk-factor blocks. A later diff finds a company's
-- prior-period filing via periodic_filings, then reads its blocks here in order.
-- block_hash (over whitespace-normalized text) keys verbatim carry-over. The
-- natural key (accession_number, section, block_index) makes re-segmentation
-- idempotent (the writer replaces a filing's blocks in one transaction). `section`
-- is 'risk_factors' for now; storing it lets MD&A and other sections slot in later.
CREATE TABLE filing_blocks (
    accession_number   TEXT NOT NULL,
    section            TEXT NOT NULL,
    block_index        INTEGER NOT NULL,
    heading            TEXT,
    block_text         TEXT NOT NULL,
    block_hash         TEXT NOT NULL,
    PRIMARY KEY (accession_number, section, block_index)
);

CREATE INDEX idx_filing_blocks_hash ON filing_blocks (block_hash);

-- periodic_ingest_cursor: singleton high-water mark for the resumable daily-index
-- scan (mirrors form4_ingest_cursor / ingest_cursor). Advances past an index date
-- only once that date is fully ingested, so an aborted tick resumes from the
-- incomplete date on the next run and fills the gap.
CREATE TABLE periodic_ingest_cursor (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    last_accession_number TEXT NOT NULL,
    last_filed_at         TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
