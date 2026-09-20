from __future__ import annotations

import math

from winnow.retrieval_eval import build_query_set, evaluate
from winnow.vector import VectorRetriever, embed_missing_signatures
from tests.conftest import insert_run, insert_job
from tests.test_retrieval import insert_sig


class FakeEmbedder:
    """Deterministic bag-of-words hashing into 384 dims -- enough for
    'similar text -> nearby vector' without downloading a model."""

    name = "fake-hash-embedder"
    dims = 384
    batch_size = 8

    def embed(self, texts):
        out = []
        for t in texts:
            v = [0.0] * self.dims
            for tok in t.lower().split():
                v[hash(tok) % self.dims] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


def test_embed_cache_is_per_distinct_text_not_per_job(conn, repo_id):
    insert_run(conn, repo_id, run_id=1)
    for jid in (1, 2, 3):
        insert_job(conn, repo_id, job_id=jid, run_id=1)
    insert_sig(conn, 1, "TimeoutError: waited <DUR>", "test:a")
    insert_sig(conn, 2, "TimeoutError: waited <DUR>", "test:a")   # same text -> same hash
    insert_sig(conn, 3, "KeyError: 'missing'", "test:b")

    n = embed_missing_signatures(conn, FakeEmbedder())
    assert n == 2
    assert embed_missing_signatures(conn, FakeEmbedder()) == 0  # idempotent


def test_vector_retriever_ranks_similar_prior_failure_first(conn, repo_id):
    insert_run(conn, repo_id, run_id=1)
    for jid in (101, 102, 103):
        insert_job(conn, repo_id, job_id=jid, run_id=1)
    insert_sig(conn, 101, "AssertionError: Tensor-likes are not close! Mismatched elements: 3 / 100", "test:a")
    insert_sig(conn, 102, "TimeoutError: test took longer than <DUR> to complete", "test:b")
    insert_sig(conn, 103, "AssertionError: Tensor-likes are not close! Mismatched elements: 9 / 100", "test:a")
    emb = FakeEmbedder()
    embed_missing_signatures(conn, emb)

    r = VectorRetriever(embedder=emb)
    ranked = r.search(conn, repo_id, before_job_id=103, query_text="AssertionError: Tensor-likes are not close! Mismatched elements: 9 / 100", k=5)
    assert ranked[0] == 101
    assert 103 not in ranked

    rep, _ = evaluate(conn, r, build_query_set(conn), k=5)
    assert rep.n_queries == 1 and rep.mrr == 1.0
