-- 020: filing_change_synthesis — the per-filing read over its material changes (ADR 0043).
--
-- Portable SQL — must compile and run identically on SQLite and Postgres. The
-- application supplies all values.
--
-- One synthesis per (filing, section, embedding model, judge_version, synthesis_version).
-- It summarizes the material change verdicts of a specific judge_version, so it is
-- keyed by that judge_version: a re-judge (new judge_version) yields a new material
-- set and therefore a new synthesis, rather than silently reinterpreting the old one.
-- synthesis_version = the synthesis model + a hash of its reduce prompt and headline
-- rule, so a prompt or rule change likewise re-derives rather than overwrites
-- (append-only, mirroring judge_version / classifier_version).
--
-- headline_direction and the *_count columns are CODE-rolled from the per-change
-- directions (the bounded-operator boundary: code aggregates, the LLM does not).
-- thesis and top_effects are the LLM reduce output; top_effects is a JSON array of
-- short strings (TEXT for portability — no array type assumed).
CREATE TABLE filing_change_synthesis (
    accession_number   TEXT NOT NULL,
    section            TEXT NOT NULL,
    model_id           TEXT NOT NULL,
    judge_version      TEXT NOT NULL,
    synthesis_version  TEXT NOT NULL,
    headline_direction TEXT NOT NULL,
    material_count     INTEGER NOT NULL,
    worse_count        INTEGER NOT NULL,
    eased_count        INTEGER NOT NULL,
    neutral_count      INTEGER NOT NULL,
    thesis             TEXT NOT NULL,
    top_effects        TEXT NOT NULL,
    synthesized_at     TEXT NOT NULL,
    PRIMARY KEY (accession_number, section, model_id, judge_version, synthesis_version)
);

CREATE INDEX idx_filing_change_synthesis_lookup
    ON filing_change_synthesis (accession_number, section, synthesis_version);
