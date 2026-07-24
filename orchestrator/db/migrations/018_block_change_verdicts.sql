-- 018: block_change_verdicts — the LLM materiality judgment per change (ADR 0042).
--
-- Portable SQL — must compile and run identically on SQLite and Postgres. The
-- application supplies all timestamps.
--
-- One verdict per (change, judge_version). judge_version = the LLM model + a hash of
-- the judge's system prompt, so the table is append-only across prompt/model changes:
-- re-judging with a new prompt writes new rows rather than overwriting the old ones,
-- keeping past judgments reproducible (mirrors classifier_version, ADR 0011). The key
-- ties back to block_changes via (accession, section, model_id, change_seq).
--
-- needs_review is code-derived (confidence below a threshold), not a model output:
-- the model judges, code decides whether to trust the judgment or route it to a
-- human — the bounded-operator boundary.
CREATE TABLE block_change_verdicts (
    accession_number  TEXT NOT NULL,
    section           TEXT NOT NULL,
    model_id          TEXT NOT NULL,
    change_seq        INTEGER NOT NULL,
    judge_version     TEXT NOT NULL,
    is_material       INTEGER NOT NULL,
    confidence        REAL NOT NULL,
    category          TEXT,
    explanation       TEXT,
    needs_review      INTEGER NOT NULL DEFAULT 0,
    judged_at         TEXT NOT NULL,
    PRIMARY KEY (accession_number, section, model_id, change_seq, judge_version)
);

CREATE INDEX idx_block_change_verdicts_lookup
    ON block_change_verdicts (accession_number, section, model_id, judge_version);
