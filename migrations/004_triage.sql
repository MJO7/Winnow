-- Phase 4: prompt-hash response cache. Every model call is keyed by the
-- sha256 of (model, system prompt, user prompt, output schema). A rerun
-- of the eval hits this table and never the API -- that is what makes
-- the eval reproducible and free to rerun rather than an anecdote.

CREATE TABLE IF NOT EXISTS llm_cache (
    prompt_hash     TEXT PRIMARY KEY,
    model           TEXT NOT NULL,
    response_json   JSONB NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms      DOUBLE PRECISION NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_llm_cache_model ON llm_cache (model);
