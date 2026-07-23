-- 016: filing_block_embeddings — vector store for change-detection (ADR 0042).
--
-- Portable SQL — must compile and run identically on SQLite and Postgres. The
-- application supplies all timestamps.
--
-- One embedding per (block, model). The diff (a later PR) reads a filing's block
-- vectors by (accession_number, section, model_id) and computes cosine similarity
-- against the prior period's — a keyed lookup, not a corpus search, so no vector
-- database is needed. The vector is stored as a JSON array of floats in a TEXT
-- column (portable + inspectable; the repo already stores structured data as JSON
-- text). model_id is part of the key so a block can carry vectors from more than
-- one model at once — embeddings are model-specific and not comparable across
-- models, so an A/B of two models simply stores both and the diff picks one. dim
-- is the vector length (whatever the model returns), recorded for validation.
CREATE TABLE filing_block_embeddings (
    accession_number  TEXT NOT NULL,
    section           TEXT NOT NULL,
    block_index       INTEGER NOT NULL,
    model_id          TEXT NOT NULL,
    dim               INTEGER NOT NULL,
    embedding_json    TEXT NOT NULL,
    embedded_at       TEXT NOT NULL,
    PRIMARY KEY (accession_number, section, block_index, model_id)
);

CREATE INDEX idx_filing_block_embeddings_model ON filing_block_embeddings (model_id);
