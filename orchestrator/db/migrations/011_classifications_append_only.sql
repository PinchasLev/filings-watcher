-- 011_classifications_append_only
--
-- Enforce at the database what was already true by design: the classifications
-- table is append-only (ADR 0011, ADR 0032). A classification is an immutable
-- record of what the classifier decided at a point in time; re-classification
-- appends a new row (a new classifier_version / run, latest wins, ADR 0028) and
-- never edits or removes an existing one. Every write path uses INSERT OR IGNORE,
-- and an audit confirmed nothing in the codebase updates or deletes a
-- classification.
--
-- Block UPDATE and DELETE so the immutability cannot be violated by accident or
-- a stray query — the same protection the taxonomy snapshot tables carry (010).
-- INSERT (including INSERT OR IGNORE) is unaffected, so the normal write path is
-- untouched. SQLite has no role-based REVOKE, so this is enforced with
-- BEFORE UPDATE/DELETE triggers that abort.

CREATE TRIGGER classifications_no_update
BEFORE UPDATE ON classifications
BEGIN
    SELECT RAISE(ABORT, 'classifications is append-only (ADR 0011/0032)');
END;

CREATE TRIGGER classifications_no_delete
BEFORE DELETE ON classifications
BEGIN
    SELECT RAISE(ABORT, 'classifications is append-only (ADR 0011/0032)');
END;
