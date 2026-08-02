-- 023: disclosure_catalog — the versioned common-mode theme catalog (calibration; extends
-- the change-detection funnel of ADR 0042/0043).
--
-- Portable SQL — must compile and run identically on SQLite and Postgres. The
-- application supplies all timestamps (UTC ISO-8601); no engine-specific defaults.
--
-- The materiality judge scores each company against only its own prior year, so the
-- macro themes the whole cohort flags in common (tariff exposure, generative-AI
-- competition, rate/refinancing pressure) inflate every card and bury each company's
-- IDIOSYNCRATIC, company-specific changes. Calibration discounts that common-mode
-- boilerplate. This table stores the catalog of those common-mode themes: one Sonnet
-- corpus-reduce over a filing season's material changes NAMES the recurring themes, gives
-- an archetype sentence for each, and estimates its prevalence. A later stage classifies
-- each change specific-vs-boilerplate against this catalog.
--
-- Unlike the taxonomy snapshot (a code-defined, deterministic contract frozen by triggers,
-- migration 010), the catalog is a DERIVED artifact of a non-deterministic LLM reduce, so
-- it is versioned by CONTENT: catalog_version embeds a hash of the extracted theme set,
-- making each distinct catalog its own immutable identity. Re-running the extractor with
-- identical output is a no-op (ON CONFLICT DO NOTHING); drifted output is a NEW version (a
-- new cut), leaving prior cuts intact. A downstream classification keys to the exact
-- catalog_version it used, so a catalog change re-opens its work (mirrors judge_version /
-- synthesis_version). No freeze triggers: a content change is already a new version by
-- construction, and re-derivation must be idempotent, not aborted.
CREATE TABLE disclosure_catalog_versions (
    catalog_version TEXT PRIMARY KEY,   -- {model}+catalog-{prompt_sha}-{content_sha}
    model_id        TEXT NOT NULL,
    corpus_label    TEXT NOT NULL,      -- the corpus reduced over, e.g. "FY2025" or "all"
    content_hash    TEXT NOT NULL,      -- sha256 of the canonical theme set
    theme_count     INTEGER NOT NULL,
    cut_at          TEXT NOT NULL        -- ISO 8601 UTC, when the catalog was cut
);

CREATE TABLE disclosure_catalog_themes (
    catalog_version TEXT NOT NULL REFERENCES disclosure_catalog_versions(catalog_version),
    theme_slug      TEXT NOT NULL,       -- snake_case identifier, e.g. tariffs_trade_policy
    archetype       TEXT NOT NULL,       -- one generic sentence stating the theme
    prevalence      INTEGER NOT NULL DEFAULT 0,  -- approx companies flagging it (model estimate)
    PRIMARY KEY (catalog_version, theme_slug)
);

CREATE INDEX idx_disclosure_catalog_themes_version
    ON disclosure_catalog_themes (catalog_version);
