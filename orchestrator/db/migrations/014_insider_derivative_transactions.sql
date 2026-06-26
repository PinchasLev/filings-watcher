-- 014: insider_derivative_transactions — the Form-4 derivative table
-- (options, warrants, convertibles), stored alongside the non-derivative
-- insider_transactions.
--
-- Portable SQL — must compile and run identically on SQLite and on Postgres.
--
-- Kept in its own table because derivative lines carry fields the
-- non-derivative table has no place for: the strike (conversion_exercise_price),
-- the exercise/expiration window, and the underlying security. `shares` is the
-- number of derivative securities; `underlying_shares` is the count of the
-- underlying they convert into. We store all transaction codes (grants A,
-- exercises M, dispositions, ...) and score narrow downstream — the same
-- store-everything rule the non-derivative table follows. See ADR 0039.
-- Idempotent on (accession_number, txn_seq), the seq local to this table.
CREATE TABLE insider_derivative_transactions (
    accession_number          TEXT NOT NULL,
    txn_seq                   INTEGER NOT NULL,
    filed_at                  TEXT NOT NULL,
    period_of_report          TEXT,
    issuer_cik                TEXT NOT NULL,
    issuer_name               TEXT,
    issuer_ticker             TEXT,
    owner_cik                 TEXT NOT NULL,
    owner_name                TEXT,
    is_director               INTEGER NOT NULL DEFAULT 0,
    is_officer                INTEGER NOT NULL DEFAULT 0,
    is_ten_percent_owner      INTEGER NOT NULL DEFAULT 0,
    is_other                  INTEGER NOT NULL DEFAULT 0,
    officer_title             TEXT,
    security_title            TEXT,
    conversion_exercise_price REAL,
    transaction_date          TEXT,
    transaction_code          TEXT,
    acquired_disposed         TEXT,
    shares                    REAL,
    price_per_share           REAL,
    transaction_value         REAL,
    exercise_date             TEXT,
    expiration_date           TEXT,
    underlying_security_title TEXT,
    underlying_shares         REAL,
    shares_owned_following    REAL,
    direct_or_indirect        TEXT,
    is_10b5_1                 INTEGER NOT NULL DEFAULT 0,
    not_subject_to_section16  INTEGER NOT NULL DEFAULT 0,
    ingested_at               TEXT NOT NULL,
    PRIMARY KEY (accession_number, txn_seq)
);

CREATE INDEX idx_insider_deriv_issuer_date
    ON insider_derivative_transactions (issuer_cik, transaction_date);
CREATE INDEX idx_insider_deriv_owner
    ON insider_derivative_transactions (owner_cik);
