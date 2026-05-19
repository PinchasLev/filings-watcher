-- 002: ingest_cursor singleton table.
--
-- Portable SQL — must compile and run identically on SQLite and on Postgres.
-- The application supplies updated_at on every write (UTC ISO-8601); no
-- engine-specific DEFAULT clause.
--
-- Singleton invariant: CHECK (id = 1) forces exactly one row. Inserts use
-- INSERT ... ON CONFLICT (id) DO UPDATE so the row is created on first
-- advance and overwritten thereafter.
--
-- Role: the cursor is a query-narrowing optimization for the daily-index
-- ingest path. Correctness against double-classification comes from the
-- accession-number primary key on filings, not from the cursor.
-- See ADR 0021.

CREATE TABLE ingest_cursor (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    last_accession_number TEXT NOT NULL,
    last_filed_at         TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
