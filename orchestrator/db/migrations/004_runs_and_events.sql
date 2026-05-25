-- 004_runs_and_events
--
-- Adds the runs ledger and the filing-level events layer (ADR 0027, ADR 0028).
--
-- runs: one row per processing pass ("run") of a single stage over one or more
--   filings. run_id is the monotonic versioning axis and the ordering for
--   "latest run wins" reads. Every deliberate re-run is a new run, regardless
--   of whether code changed — the LLM is an uncontrolled source of variation,
--   so identical configuration may still produce different output (ADR 0028).
--   The classifier/reducer version strings are run metadata here, not row
--   identity.
--
-- events: filing-level events produced by the reduce stage, each collating one
--   or more per-Item classifications. Identity within a run is
--   (run_id, accession_number, anchor_item_number); the current view of a
--   filing is the complete output of its latest run, selected wholesale.
--
-- event_classifications: the join from an event to the exact classification
--   rows it collated — ADR 0011's "IDs of contributing classification rows"
--   reproducibility contract, realized.
--
-- Existing tables are untouched: no historical retrofit (ADR 0028). Filings
-- classified before this migration carry no run_id and surface only through
-- the pinned join; they are brought current by an explicit re-run, never
-- migrated.

CREATE TABLE runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stage            TEXT NOT NULL,            -- 'classify' | 'reduce'
    model            TEXT,                     -- model name; nullable for non-LLM stages
    config_version   TEXT NOT NULL,            -- classifier_version / reducer_version (model + prompt hash)
    taxonomy_version TEXT NOT NULL,
    source_run_id    INTEGER REFERENCES runs(run_id),  -- for reduce: the classify run consumed
    status           TEXT NOT NULL,            -- 'running' | 'succeeded' | 'failed' | 'partial'
    started_at       TEXT NOT NULL,            -- ISO 8601 UTC
    finished_at      TEXT,                     -- ISO 8601 UTC; NULL while running
    notes            TEXT                      -- optional freeform: trigger, scope
);

CREATE INDEX idx_runs_stage ON runs (stage, run_id DESC);

CREATE TABLE events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             INTEGER NOT NULL REFERENCES runs(run_id),
    accession_number   TEXT NOT NULL REFERENCES filings(accession_number),
    anchor_item_number TEXT,                   -- primary substantive Item; NULL for whole-filing event
    event_type         TEXT NOT NULL,
    event_domain       TEXT NOT NULL,          -- denormalized via domain_for(event_type)
    is_material        INTEGER NOT NULL,       -- 0 / 1
    confidence         REAL NOT NULL,
    summary            TEXT NOT NULL
);

-- Within a run, one event per (accession, anchor). The COALESCE lets a
-- whole-filing event (anchor NULL) participate in the constraint, mirroring
-- the classifications unique index.
CREATE UNIQUE INDEX idx_events_unique
    ON events (run_id, accession_number, COALESCE(anchor_item_number, ''));

-- "Latest run per filing" reads filter to MAX(run_id) per accession.
CREATE INDEX idx_events_accession_run ON events (accession_number, run_id DESC);

CREATE TABLE event_classifications (
    event_id          INTEGER NOT NULL REFERENCES events(id),
    classification_id INTEGER NOT NULL REFERENCES classifications(id),
    PRIMARY KEY (event_id, classification_id)
);
