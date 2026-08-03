-- 025: risk_realizations — the radar's first "track update" (materialization tracking).
--
-- Portable SQL — must compile and run identically on SQLite and Postgres. The
-- application supplies all timestamps (UTC ISO-8601); no engine-specific defaults.
--
-- The Risk Radar detects a company-specific risk in a 10-K (change_specificity,
-- migration 024). This stage TRACKS that risk forward: for each flagged specific risk,
-- it judges whether a subsequent 8-K/6-K filing DIRECTLY realizes it — uplifting the risk
-- from declared (hypothetical) to materialized. is_realized=1 records that a specific 8-K
-- disclosure draws a direct line to this risk; realizing_accession/event_type/item name
-- that 8-K, and evidence states how it realizes THIS risk.
--
-- The flagged risk's identity (accession, section, model_id, change_seq) is the ANCHOR the
-- track hangs on — it ties back to block_change_verdicts / change_specificity. One verdict
-- per (risk, judge_version, realization_version): keyed on judge_version because that
-- defines the material risk set, and on realization_version (model + prompt hash) so a
-- prompt/model change re-derives rather than reinterprets — append-only, mirroring the
-- judge / synthesis / specificity stages. It is deliberately NOT keyed on catalog or
-- specificity versions: realization is about the risk->8-K link, independent of catalog
-- churn (specificity is only the filter for which risks are tracked).
--
-- checked_through is the latest 8-K filing_date considered when the verdict was cut, so a
-- "not realized yet" verdict can re-open once a newer 8-K arrives — the risk keeps being
-- tracked as filings land, rather than being judged once and frozen.
CREATE TABLE risk_realizations (
    accession_number     TEXT NOT NULL,
    section              TEXT NOT NULL,
    model_id             TEXT NOT NULL,
    change_seq           INTEGER NOT NULL,
    judge_version        TEXT NOT NULL,
    realization_version  TEXT NOT NULL,
    is_realized          INTEGER NOT NULL,   -- 1 = a subsequent 8-K directly realizes this risk
    realizing_accession  TEXT,               -- the 8-K that realizes it; NULL when not realized
    realizing_event_type TEXT,
    realizing_item       TEXT,               -- the 8-K item that draws the line
    evidence             TEXT,
    confidence           REAL,
    checked_through      TEXT,               -- max 8-K filing_date considered (re-check watermark)
    judged_at            TEXT NOT NULL,
    PRIMARY KEY (accession_number, section, model_id, change_seq,
                 judge_version, realization_version)
);

CREATE INDEX idx_risk_realizations_lookup
    ON risk_realizations (accession_number, section, model_id,
                          judge_version, realization_version);
