-- 013: insider_filings envelope + form4_ingest_cursor.
--
-- Portable SQL — must compile and run identically on SQLite and on Postgres.
-- The application supplies all timestamps (UTC ISO-8601); no engine-specific
-- DEFAULT clauses.
--
-- insider_filings: one row per PROCESSED Form 4, written even when the filing
-- yields zero stored transactions (option-only filings, or a fetched document
-- with no usable ownership XML). This is the dedup ANCHOR and completeness
-- ledger for the Form-4 ingest — the equivalent of what the `filings` table is
-- for the 8-K path. Dedup and "is this index date fully ingested?" both key off
-- THIS table, not insider_transactions (which only has rows when there are
-- non-derivative transactions, so option-only Form 4s would otherwise re-fetch
-- forever and never let a date read as complete). issuer_cik / owner_cik are
-- nullable so an unparseable-but-fetched filing still leaves an anchor row.
-- See ADR 0038.
CREATE TABLE insider_filings (
    accession_number         TEXT PRIMARY KEY,
    filed_at                 TEXT NOT NULL,
    period_of_report         TEXT,
    issuer_cik               TEXT,
    issuer_name              TEXT,
    issuer_ticker            TEXT,
    owner_cik                TEXT,
    owner_name               TEXT,
    is_director              INTEGER NOT NULL DEFAULT 0,
    is_officer               INTEGER NOT NULL DEFAULT 0,
    is_ten_percent_owner     INTEGER NOT NULL DEFAULT 0,
    is_other                 INTEGER NOT NULL DEFAULT 0,
    officer_title            TEXT,
    is_10b5_1                INTEGER NOT NULL DEFAULT 0,
    not_subject_to_section16 INTEGER NOT NULL DEFAULT 0,
    parsed                   INTEGER NOT NULL DEFAULT 1,
    non_derivative_count     INTEGER NOT NULL DEFAULT 0,
    derivative_count         INTEGER NOT NULL DEFAULT 0,
    ingested_at              TEXT NOT NULL
);

CREATE INDEX idx_insider_filings_issuer_date ON insider_filings (issuer_cik, filed_at);
CREATE INDEX idx_insider_filings_filed_at ON insider_filings (filed_at);

-- Anchor already-ingested filings: every distinct accession in
-- insider_transactions becomes an envelope row, so dedup (now keyed off
-- insider_filings) recognizes them and they are not re-fetched. The
-- per-accession columns are constant across that accession's rows, so the
-- aggregates just collapse the duplicates; non_derivative_count is the row
-- count. Option-only filings from before this migration have no rows here and
-- so are not anchored — they re-fetch once on their next scan, which is
-- correct (we never stored them).
INSERT INTO insider_filings (
    accession_number, filed_at, period_of_report, issuer_cik, issuer_name, issuer_ticker,
    owner_cik, owner_name, is_director, is_officer, is_ten_percent_owner, is_other,
    officer_title, is_10b5_1, not_subject_to_section16, parsed,
    non_derivative_count, derivative_count, ingested_at
)
SELECT
    accession_number, MIN(filed_at), MIN(period_of_report), MIN(issuer_cik),
    MIN(issuer_name), MIN(issuer_ticker), MIN(owner_cik), MIN(owner_name),
    MAX(is_director), MAX(is_officer), MAX(is_ten_percent_owner), MAX(is_other),
    MIN(officer_title), MAX(is_10b5_1), MAX(not_subject_to_section16), 1,
    COUNT(*), 0, MIN(ingested_at)
FROM insider_transactions
GROUP BY accession_number;

-- form4_ingest_cursor: singleton high-water mark for the cursor-driven,
-- resumable daily-index scan (mirrors ingest_cursor for the 8-K path, ADR
-- 0021). The cursor advances past an index date ONLY once that date is fully
-- ingested, so an aborted tick resumes from the incomplete date on the next
-- run. Kept separate from ingest_cursor because Form-4 ingest progresses
-- independently of the 8-K/6-K path.
CREATE TABLE form4_ingest_cursor (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    last_accession_number TEXT NOT NULL,
    last_filed_at         TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
