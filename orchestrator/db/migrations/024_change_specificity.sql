-- 024: change_specificity — per-change specific-vs-boilerplate verdict (calibration; extends
-- the change-detection funnel of ADR 0042/0043, consumes the catalog of migration 023).
--
-- Portable SQL — must compile and run identically on SQLite and Postgres. The
-- application supplies all timestamps (UTC ISO-8601); no engine-specific defaults.
--
-- The judge (migration 018) decides whether a change is material; this stage decides, for
-- each material change, whether it is a COMPANY-SPECIFIC development (a real idiosyncratic
-- event/fact about this company) or an instance of a COMMON-MODE catalog theme (macro
-- boilerplate the whole cohort discloses). is_specific is the filter a later stage uses to
-- surface only the company-specific changes and discount the boilerplate; matched_theme
-- names the catalog theme when a change is boilerplate.
--
-- One verdict per (change, judge_version, catalog_version, specificity_version): keyed on
-- judge_version because "the material set" is defined relative to it, on catalog_version
-- because a new catalog re-opens the classification, and on specificity_version (model +
-- prompt hash) so a prompt/model change re-classifies rather than reinterprets — append-only
-- across all three, mirroring block_change_verdicts / filing_change_synthesis. The change
-- identity (accession, section, model_id, change_seq) ties back to block_changes.
CREATE TABLE change_specificity (
    accession_number    TEXT NOT NULL,
    section             TEXT NOT NULL,
    model_id            TEXT NOT NULL,
    change_seq          INTEGER NOT NULL,
    judge_version       TEXT NOT NULL,
    catalog_version     TEXT NOT NULL,
    specificity_version TEXT NOT NULL,
    is_specific         INTEGER NOT NULL,   -- 1 = company-specific, 0 = common-mode boilerplate
    matched_theme       TEXT,               -- catalog theme_slug when boilerplate; NULL when specific
    confidence          REAL NOT NULL,
    explanation         TEXT,
    classified_at       TEXT NOT NULL,
    PRIMARY KEY (accession_number, section, model_id, change_seq,
                 judge_version, catalog_version, specificity_version)
);

CREATE INDEX idx_change_specificity_lookup
    ON change_specificity (accession_number, section, model_id,
                           judge_version, catalog_version, specificity_version);
