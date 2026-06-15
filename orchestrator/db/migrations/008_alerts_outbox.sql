-- 008_alerts_outbox
--
-- The alerting outbox (ADR 0031). Any component that detects a push-worthy
-- condition raises an alert by INSERTing one row here — ideally in the same
-- transaction as the state change that warrants it (a transactional outbox:
-- the alert and the fact it reports commit together, so neither a lost alert
-- nor a phantom alert is possible). A single standalone `alarm-drain` CLI is
-- the only component that reads this table and delivers rows to Discord; this
-- migration adds only the queue.
--
-- `severity` routes delivery to one of two Discord channels: 'alert'
-- (needs-human-action) or 'info' (situational awareness). It is a plain TEXT
-- discriminator, not an enum, for SQLite/Postgres portability (ADR 0008); the
-- emit() helper is the single writer and constrains the value.
--
-- `title` is the human-facing headline; `body` an optional longer detail;
-- `fields_json` the structured context (accession, counts, error class) the
-- drainer renders into the message. `dedup_key` is an optional caller-supplied
-- key the drainer uses to suppress re-paging a standing condition (e.g. one
-- cost-cap alert per UTC day) — NULL means "always deliver, never coalesce".
--
-- Delivery state lives on the row: `delivered_at` NULL = not yet delivered
-- (the drainer's work set); `attempts` and `last_error` record delivery
-- retries (distinct from the classify `classify_attempts` counter — those
-- count classification failures, these count POST failures). The drainer and
-- its dedup logic land in a later PR; this schema is forward-compatible with
-- them.
--
-- RETENTION (deferred): like `llm_calls` (005), delivered rows are telemetry-
-- shaped — append-only, time-bounded in usefulness. A future prune of
-- long-delivered rows (or a move to a telemetry store) is the same retention
-- question 005 flagged; out of scope here.

CREATE TABLE alerts_outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,                 -- ISO 8601 UTC, when emitted
    severity      TEXT    NOT NULL,                 -- 'alert' | 'info'
    title         TEXT    NOT NULL,                 -- human-facing headline
    body          TEXT,                             -- optional longer detail
    fields_json   TEXT    NOT NULL DEFAULT '{}',    -- structured context (JSON object)
    dedup_key     TEXT,                             -- optional coalescing key; NULL = always send
    delivered_at  TEXT,                             -- NULL = undelivered (the drainer's work set)
    attempts      INTEGER NOT NULL DEFAULT 0,       -- delivery (POST) attempts
    last_error    TEXT                              -- last delivery failure, for diagnosis
);

-- The drainer's hot read is "undelivered rows, oldest first". A partial index
-- on the undelivered subset (supported identically by SQLite and Postgres)
-- keeps that scan tiny as delivered rows accumulate.
CREATE INDEX idx_alerts_outbox_undelivered
    ON alerts_outbox (created_at)
    WHERE delivered_at IS NULL;
