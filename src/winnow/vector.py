"""Embedding pipeline and pgvector retriever (Phase 3).

Embedder is a small protocol so the local sentence-transformers model
used here can be swapped for a hosted one (Bedrock Titan in Phase 5)
without touching the cache or the retriever. Cost accounting for the
local model is zero dollars and wall-clock only; that is stated in the
benchmark rather than hidden.
"""
from __future__ import annotations

import logging
from typing import Protocol, Sequence

import psycopg
from pgvector.psycopg import register_vector

logger = logging.getLogger("winnow.vector")

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DIMS = 384


class Embedder(Protocol):
    name: str
    dims: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str = DEFAULT_MODEL, batch_size: int = 64):
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self.name = model_name
        self.dims = int(self._model.get_sentence_embedding_dimension())
        self.batch_size = batch_size

    def embed(self, texts):
        vecs = self._model.encode(list(texts), batch_size=self.batch_size, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vecs]


def embed_missing_signatures(conn: psycopg.Connection, embedder: Embedder) -> int:
    """Embeds every distinct retrieval_text without a cached vector for
    this model. Batched; idempotent; a rerun after a crash costs nothing
    for what already landed."""
    register_vector(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT fs.retrieval_hash, fs.retrieval_text
            FROM failure_signatures fs
            LEFT JOIN signature_embeddings e
              ON e.retrieval_hash = fs.retrieval_hash AND e.model = %s
            WHERE fs.retrieval_text IS NOT NULL AND e.retrieval_hash IS NULL
            """,
            (embedder.name,),
        )
        todo = cur.fetchall()

    logger.info("model=%s distinct texts to embed: %d", embedder.name, len(todo))
    done = 0
    for i in range(0, len(todo), embedder.batch_size):
        batch = todo[i : i + embedder.batch_size]
        vecs = embedder.embed([t for _, t in batch])
        with conn.cursor() as cur:
            for (h, _), v in zip(batch, vecs):
                cur.execute(
                    """
                    INSERT INTO signature_embeddings (retrieval_hash, model, dims, embedding)
                    VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
                    """,
                    (h, embedder.name, embedder.dims, v),
                )
        conn.commit()
        done += len(batch)
        if done % (embedder.batch_size * 10) == 0:
            logger.info("embedded %d/%d", done, len(todo))
    return done


class VectorRetriever:
    """HNSW cosine search, with the repo and recency filters in the same
    statement as the ORDER BY -- the point of keeping vectors in Postgres."""

    name = "pgvector_hnsw"

    def __init__(self, embedder: Embedder | None = None):
        self._embedder = embedder
        self._registered: set[int] = set()

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = SentenceTransformerEmbedder()
        return self._embedder

    def search(self, conn, repo_id, before_job_id, query_text, k):
        if id(conn) not in self._registered:
            register_vector(conn)
            self._registered.add(id(conn))
        vec = self.embedder.embed([query_text])[0]
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT fs.job_id
                FROM failure_signatures fs
                JOIN jobs j ON j.job_id = fs.job_id
                JOIN signature_embeddings e
                  ON e.retrieval_hash = fs.retrieval_hash AND e.model = %s
                WHERE j.repo_id = %s AND fs.job_id < %s
                ORDER BY e.embedding <=> %s::vector, fs.job_id DESC
                LIMIT %s
                """,
                (self.embedder.name, repo_id, before_job_id, vec, k),
            )
            return [r[0] for r in cur.fetchall()]
