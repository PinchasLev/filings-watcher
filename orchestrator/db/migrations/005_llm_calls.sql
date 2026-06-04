-- 005_llm_calls
--
-- Adds the per-LLM-call observability surface required by ADR 0029. One row
-- per Anthropic call: model, stage, token-count breakdown (including the
-- cache-read and cache-creation subsets for ADR 0022's prompt-cache pricing),
-- and an estimated USD cost computed at insert time from a per-model pricing
-- table. The accession the call was made against is recorded when known.
--
-- Naming convention: this is the LLM-call log. Tokens are the engineering
-- metric we control; cost is a derived value. Both are recorded as columns;
-- the daily cost aggregate gates the pre-tick cap check, the daily token
-- aggregate is available for engineering analysis (caching effectiveness,
-- prompt-size trends).
--
-- RETENTION (deferred): rows in this table are telemetry-shaped — append-only,
-- high-volume, time-bounded in usefulness — not transactional. Keeping them
-- here is pragmatic at v0 scale (single-host SQLite, dev-friendly queryability,
-- no extra infra) but conflates two retention curves: filings should be kept
-- forever, per-LLM-call rows should not. The disciplined long-term shape is
-- one of: (a) aggregate-in-DB / detail-in-logs — replace per-call rows with a
-- small `llm_usage_daily` aggregate, push per-call detail to journald and an
-- eventual observability backend; or (b) a separate telemetry store. A future
-- ADR amends this — see the memory note `telemetry-vs-transactional` and the
-- broader observability surface scoped by ADR 0029.

CREATE TABLE llm_calls (
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
CREATE INDEX idx_llm_calls_emitted_at ON llm_calls (emitted_at);
