-- 010_taxonomy_snapshots
--
-- Persist each taxonomy version as a durable, queryable snapshot (ADR 0032).
-- Knowing what a classification *decided* (its stored leaf) is not the same as
-- knowing what it was *allowed to choose from*; that choice-set lived only in
-- taxonomy.py at the commit a `taxonomy_version` was current. These tables
-- record the full definition of each version so any historical classification
-- joins to the exact menu it faced (on `taxonomy_version` + `event_type`),
-- reproducibly and without git archaeology.
--
-- Three tables mirror the two tiers plus a version anchor:
--   * taxonomy_versions  — one row per cut version (the anchor), carrying the
--     content hash that binds the version label to its definition, and the
--     parsed major/minor (ADR 0032's major.minor semantics; "v1" reads as 1.0).
--   * taxonomy_domains   — tier-1, the coarse contract, per version.
--   * taxonomy_leaves    — tier-2, the menu, per version; rolls up to a domain.
--
-- A version's snapshot is FROZEN once cut: the populate path writes the anchor
-- and all of a version's domain/leaf rows in one transaction (atomic creation),
-- and the rows are append-only — UPDATE and DELETE are blocked by the triggers
-- below so a recorded label can never be silently changed or deleted. SQLite has
-- no role-based REVOKE, so this is enforced with BEFORE UPDATE/DELETE triggers
-- that abort. (Appending a row to an already-cut version is left to the
-- content-hash integrity check, which makes any such change tamper-evident; see
-- the taxonomy_snapshot module.)

CREATE TABLE taxonomy_versions (
    taxonomy_version TEXT PRIMARY KEY,         -- e.g. "v1", "v1.1", "v2"
    major            INTEGER NOT NULL,         -- parsed for range queries
    minor            INTEGER NOT NULL,
    content_hash     TEXT NOT NULL,            -- sha256 of the full definition
    cut_at           TEXT NOT NULL             -- ISO 8601 UTC, when snapshotted
);

CREATE TABLE taxonomy_domains (
    taxonomy_version TEXT NOT NULL REFERENCES taxonomy_versions(taxonomy_version),
    domain           TEXT NOT NULL,            -- EventDomain value, e.g. "financial"
    description      TEXT,                      -- tier-1 description (may be NULL)
    PRIMARY KEY (taxonomy_version, domain)
);

CREATE TABLE taxonomy_leaves (
    taxonomy_version TEXT NOT NULL REFERENCES taxonomy_versions(taxonomy_version),
    leaf             TEXT NOT NULL,            -- EventType value, e.g. "ma_activity"
    description      TEXT NOT NULL,            -- the leaf description shown to the model
    domain           TEXT NOT NULL,            -- the domain it rolls up to
    PRIMARY KEY (taxonomy_version, leaf),
    FOREIGN KEY (taxonomy_version, domain)
        REFERENCES taxonomy_domains(taxonomy_version, domain)
);

-- Append-only enforcement: a cut snapshot is immutable. Block UPDATE and DELETE
-- on all three tables (INSERT is how a version is cut; it is allowed).

CREATE TRIGGER taxonomy_versions_no_update
BEFORE UPDATE ON taxonomy_versions
BEGIN
    SELECT RAISE(ABORT, 'taxonomy_versions is append-only (ADR 0032)');
END;

CREATE TRIGGER taxonomy_versions_no_delete
BEFORE DELETE ON taxonomy_versions
BEGIN
    SELECT RAISE(ABORT, 'taxonomy_versions is append-only (ADR 0032)');
END;

CREATE TRIGGER taxonomy_domains_no_update
BEFORE UPDATE ON taxonomy_domains
BEGIN
    SELECT RAISE(ABORT, 'taxonomy_domains is append-only (ADR 0032)');
END;

CREATE TRIGGER taxonomy_domains_no_delete
BEFORE DELETE ON taxonomy_domains
BEGIN
    SELECT RAISE(ABORT, 'taxonomy_domains is append-only (ADR 0032)');
END;

CREATE TRIGGER taxonomy_leaves_no_update
BEFORE UPDATE ON taxonomy_leaves
BEGIN
    SELECT RAISE(ABORT, 'taxonomy_leaves is append-only (ADR 0032)');
END;

CREATE TRIGGER taxonomy_leaves_no_delete
BEFORE DELETE ON taxonomy_leaves
BEGIN
    SELECT RAISE(ABORT, 'taxonomy_leaves is append-only (ADR 0032)');
END;
