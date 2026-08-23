-- The realization judge must cite a quote copied verbatim from the realizing 8-K, and code
-- verifies that quote is a real span before a materialization is surfaced. That verified
-- citation was previously discarded after the check: the column below keeps it, so the claim
-- rendered on the page can be audited against the filing text it rests on, and so the evidence
-- sentence can be checked against the same span it was derived from.
ALTER TABLE risk_realizations ADD COLUMN quote TEXT NOT NULL DEFAULT '';
