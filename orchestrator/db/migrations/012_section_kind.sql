-- 012_section_kind
--
-- Add the document class of a classified section (ADR 0034). A 6-K is a
-- catch-all that carries both 8-K-equivalent events and 10-Q/10-K-equivalent
-- periodic financial reports we deliberately defer. `section_kind` records which
-- a section is, distinct from its event_type (which stays the material-event
-- taxonomy): 'event' sections are collated into the events layer as before;
-- 'periodic_report' sections are recorded but deferred — they are the queryable
-- hand-off for a future periodic-content extraction pass.
--
-- Additive: existing rows and every 8-K classification default to 'event', so no
-- backfill and no change to current behavior. The column is NOT NULL with a
-- default, which SQLite applies to existing rows on ALTER.

ALTER TABLE classifications ADD COLUMN section_kind TEXT NOT NULL DEFAULT 'event';
