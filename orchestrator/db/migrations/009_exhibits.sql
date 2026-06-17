-- 009_exhibits
--
-- Store the EX-99.* exhibits ("Additional Exhibits" — press releases and
-- supplemental material) furnished with an 8-K, so the classifier can read the
-- substance that a thin Item 7.01/8.01 body just points at ("...furnished as
-- Exhibit 99.1"). The sub-number (99.1, 99.2, ...) carries no content meaning —
-- it is just the order the filer attached things — so we capture all of them and
-- let the classifier judge relevance (ADR 0031's bounded-operator split: the LLM
-- does semantic judgment, deterministic code does fetching/storage).
--
-- `exhibits_json` is a JSON array of {exhibit_type, document, url, text}. The
-- FULL exhibit text is stored, untruncated: any volume budget applied at
-- classification time is a property of the prompt, not of what we retain — so a
-- later reprocess (e.g. reading more of a long exhibit) needs no EDGAR re-fetch.
-- NULL on rows written before this migration (forward-only; no backfill yet).
--
-- Like body_text (001) and llm_calls (005), this is append-once content with a
-- retention question deferred to the same future prune.

ALTER TABLE filings ADD COLUMN exhibits_json TEXT;
