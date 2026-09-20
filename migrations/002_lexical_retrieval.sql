-- Phase 2: lexical retrieval baseline over failure signatures.
--
-- retrieval_text is what both the lexical baseline and (Phase 3) the
-- embedding see: exception type + message skeleton only. The test node
-- id and stack frames are deliberately NOT in it -- they are the
-- ground-truth key (failure_key), and putting the answer in the query
-- would make every retriever look perfect on exact test-name matches.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

ALTER TABLE failure_signatures
    ADD COLUMN IF NOT EXISTS retrieval_text TEXT,
    ADD COLUMN IF NOT EXISTS failure_key TEXT;

UPDATE failure_signatures
SET retrieval_text = COALESCE(exception_type || ': ', '') || COALESCE(message_skeleton, '')
WHERE retrieval_text IS NULL;

ALTER TABLE failure_signatures
    ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('simple', COALESCE(retrieval_text, ''))) STORED;

CREATE INDEX IF NOT EXISTS idx_sig_tsv ON failure_signatures USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_sig_trgm ON failure_signatures USING GIN (retrieval_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_sig_failure_key ON failure_signatures (failure_key);
