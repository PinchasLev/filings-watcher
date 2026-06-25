-- 012_insider_transactions
--
-- Form 4 (Section 16 insider transactions) — the structured-extraction foundation
-- for the insider-activity signal (product-monetization-direction). Unlike 8-K/6-K,
-- a Form 4 is filed as a structured `ownershipDocument` XML, so extraction is
-- deterministic parsing, NOT LLM classification (bounded-operator: code parses the
-- facts; the LLM only ever touches the already-classified event layer, via a later
-- cross-stream join). This table is therefore append-once structured data with no
-- classifier_version / cost involved.
--
-- One row per non-derivative transaction (a filing can report several). The signal
-- focuses on open-market buys (transaction_code='P', acquired_disposed='A'), but we
-- store every code — sells are needed for the bidirectional "sold-before-bad-news"
-- and "sold-after-event" patterns. Derivative (option) transactions are deferred.
--
-- `is_10b5_1` (from the XML `<aff10b5One>` affirmation) separates scheduled/routine
-- transactions from discretionary ones — the routine-vs-discretionary cut that makes
-- a sell signal meaningful. Reliable on recent filings; older filings may carry it
-- only in free-text footnotes (best-effort).
--
-- PK (accession_number, txn_seq) makes re-ingest idempotent (INSERT OR IGNORE).
-- US-domestic only by construction: foreign private issuers (6-K filers) are exempt
-- from Section 16 and file no Form 4.

CREATE TABLE IF NOT EXISTS insider_transactions (
    accession_number        TEXT    NOT NULL,
    txn_seq                 INTEGER NOT NULL,
    filed_at                TEXT,
    period_of_report        TEXT,
    issuer_cik              TEXT    NOT NULL,
    issuer_name             TEXT,
    issuer_ticker           TEXT,
    owner_cik               TEXT    NOT NULL,
    owner_name              TEXT,
    is_director             INTEGER NOT NULL DEFAULT 0,
    is_officer              INTEGER NOT NULL DEFAULT 0,
    is_ten_percent_owner    INTEGER NOT NULL DEFAULT 0,
    is_other                INTEGER NOT NULL DEFAULT 0,
    officer_title           TEXT,
    transaction_date        TEXT,
    security_title          TEXT,
    transaction_code        TEXT,
    acquired_disposed       TEXT,
    shares                  REAL,
    price_per_share         REAL,
    transaction_value       REAL,
    shares_owned_following  REAL,
    direct_or_indirect      TEXT,
    is_10b5_1               INTEGER NOT NULL DEFAULT 0,
    not_subject_to_section16 INTEGER NOT NULL DEFAULT 0,
    ingested_at             TEXT    NOT NULL,
    PRIMARY KEY (accession_number, txn_seq)
);

-- The cross-stream join (insider activity x classified events) and the cluster
-- aggregation both key on issuer + time.
CREATE INDEX IF NOT EXISTS idx_insider_issuer_date ON insider_transactions (issuer_cik, transaction_date);
CREATE INDEX IF NOT EXISTS idx_insider_owner ON insider_transactions (owner_cik);
