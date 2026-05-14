-- 001: initial schema for filings and classifications.
--
-- Portable SQL — must compile and run identically on SQLite and on Postgres,
-- since persistence will eventually migrate per ADR 0008. Avoid engine-specific
-- types and features.
--
-- Versioning model:
--   - classifications are append-only and version-tagged
--   - never updated in place; new classifier versions produce new rows
--   - UNIQUE INDEX over (accession_number, COALESCE(item_number, ''), classifier_version)
--     prevents same-version duplicates while supporting whole-filing rows (where
--     item_number IS NULL)
-- See ADR 0011.

CREATE TABLE filings (
    accession_number     TEXT PRIMARY KEY,
    cik                  TEXT NOT NULL,
    ticker               TEXT,
    company_name         TEXT NOT NULL,
    form                 TEXT NOT NULL,
    filing_date          TEXT NOT NULL,   -- ISO YYYY-MM-DD
    report_date          TEXT,
    primary_document     TEXT NOT NULL,
    primary_document_url TEXT NOT NULL,
    items_json           TEXT NOT NULL,   -- JSON-encoded list of FilingItem
    body_text            TEXT,            -- parsed body; nullable when only metadata fetched
    body_size_bytes      INTEGER,
    sections_json        TEXT,            -- JSON-encoded list of ItemSection
    fetched_at           TEXT NOT NULL
);

CREATE INDEX idx_filings_cik_date    ON filings (cik, filing_date DESC);
CREATE INDEX idx_filings_ticker_date ON filings (ticker, filing_date DESC);

CREATE TABLE classifications (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_number     TEXT NOT NULL REFERENCES filings(accession_number),
    item_number          TEXT,            -- NULL for whole-filing classifications
    item_title           TEXT,
    event_type           TEXT NOT NULL,
    event_domain         TEXT NOT NULL,   -- denormalized for cheap dashboard queries
    is_material          INTEGER NOT NULL, -- 0 / 1
    confidence           REAL NOT NULL,
    reasoning            TEXT NOT NULL,
    classifier_version   TEXT NOT NULL,
    taxonomy_version     TEXT NOT NULL,
    classified_at        TEXT NOT NULL    -- ISO 8601 with tz
);

CREATE UNIQUE INDEX idx_classifications_unique
    ON classifications (accession_number, COALESCE(item_number, ''), classifier_version);

CREATE INDEX idx_classifications_accession       ON classifications (accession_number);
CREATE INDEX idx_classifications_domain_date     ON classifications (event_domain, classified_at DESC);
CREATE INDEX idx_classifications_event_type_date ON classifications (event_type, classified_at DESC);
