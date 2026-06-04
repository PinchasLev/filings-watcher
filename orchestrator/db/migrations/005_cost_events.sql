-- 005_cost_events
--
-- Adds the cost-observability surface required by ADR 0029. One row per
-- Anthropic call: model, stage, token counts (including the cache-read and
-- cache-creation subsets for ADR 0022's prompt-cache pricing), an estimated
-- USD cost computed at insert time from a per-model pricing table, and the
-- accession the call was made against (nullable; non-filing-bound calls have
-- no anchor).
--
-- The daily aggregate over this table is the pre-tick check that gates ingest
-- when spend exceeds the operator-configured cap. The same surface carries
-- the normal-state baseline ADR 0029 requires for pattern recognition: per-
-- call records are queryable by stage, model, and accession for retrospective
-- analysis without inventing a separate metrics store.

CREATE TABLE cost_events (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    emitted_at               TEXT    NOT NULL,            -- ISO 8601 UTC
    model                    TEXT    NOT NULL,
    stage                    TEXT    NOT NULL,            -- 'classify' | 'reduce'
    accession_number         TEXT,                        -- nullable for non-filing-bound calls
    input_tokens             INTEGER NOT NULL,            -- total input tokens (incl. cached)
    output_tokens            INTEGER NOT NULL,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,  -- subset of input_tokens served from cache
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,  -- subset of input_tokens that wrote the cache
    estimated_cost_usd       REAL    NOT NULL             -- computed at insert from the pricing table
);

-- Pre-tick aggregate scans by emitted_at; the daily check is the hot read path.
CREATE INDEX idx_cost_events_emitted_at ON cost_events (emitted_at);
