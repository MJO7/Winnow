-- Phase 3: pgvector embedding index over failure signatures.
-- Requires the pgvector/pgvector:pg16 image (docker-compose.yml).
--
-- One embedding per unique (retrieval_text, model), keyed by the md5
-- of the retrieval_text -- never one per job. A signature that recurs
-- across 400 jobs costs one embedding call. Retrieval joins back to
-- failure_signatures on the same hash.

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE failure_signatures
    ADD COLUMN IF NOT EXISTS retrieval_hash TEXT
    GENERATED ALWAYS AS (md5(COALESCE(retrieval_text, ''))) STORED;

CREATE INDEX IF NOT EXISTS idx_sig_retrieval_hash ON failure_signatures (retrieval_hash);

CREATE TABLE IF NOT EXISTS signature_embeddings (
    retrieval_hash  TEXT NOT NULL,
    model           TEXT NOT NULL,
    dims            INTEGER NOT NULL,
    embedding       vector(384) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (retrieval_hash, model)
);

-- HNSW over cosine distance. m/ef_construction are pgvector defaults
-- (16/64); tuned only if the recall gap vs exact search is measured to
-- matter, which at this corpus size it will not.
CREATE INDEX IF NOT EXISTS idx_sig_emb_hnsw
    ON signature_embeddings USING hnsw (embedding vector_cosine_ops);
