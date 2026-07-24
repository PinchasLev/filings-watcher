-- 017: filing_diffs + block_changes — the change-detection diff shortlist (ADR 0042).
--
-- Portable SQL — must compile and run identically on SQLite and Postgres. The
-- application supplies all timestamps.
--
-- A diff pairs a filing with its prior comparable (same company, next-earlier parsed
-- period), aligns their risk-factor block vectors, and records what changed.
--
-- filing_diffs is one row per (filing, section, model) diff: the pairing and the
-- per-category counts. Keyed so re-running a diff upserts rather than duplicates.
CREATE TABLE filing_diffs (
    accession_number       TEXT NOT NULL,
    section                TEXT NOT NULL,
    model_id               TEXT NOT NULL,
    prior_accession_number TEXT NOT NULL,
    added_count            INTEGER NOT NULL,
    changed_count          INTEGER NOT NULL,
    carried_count          INTEGER NOT NULL,
    dropped_count          INTEGER NOT NULL,
    computed_at            TEXT NOT NULL,
    PRIMARY KEY (accession_number, section, model_id)
);

-- block_changes is the shortlist: one row per added/changed/dropped block (carried
-- blocks are only counted, not listed). change_seq is the 0-based ordinal within the
-- diff, giving a null-free composite key so the writer can replace a diff's changes
-- idempotently (delete + insert). current_block_index is null for a dropped block;
-- prior_block_index is null for an added block. similarity is the best-match cosine
-- (null when there is no counterpart at all). A later PR reads this shortlist, loads
-- each side's text, and judges materiality.
CREATE TABLE block_changes (
    accession_number       TEXT NOT NULL,
    section                TEXT NOT NULL,
    model_id               TEXT NOT NULL,
    change_seq             INTEGER NOT NULL,
    change_type            TEXT NOT NULL,
    current_block_index    INTEGER,
    prior_block_index      INTEGER,
    prior_accession_number TEXT NOT NULL,
    similarity             REAL,
    PRIMARY KEY (accession_number, section, model_id, change_seq)
);

CREATE INDEX idx_block_changes_lookup ON block_changes (accession_number, section, model_id);
