from __future__ import annotations

import pytest

from winnow.retrieval import FullTextRetriever, TrigramRetriever
from winnow.retrieval_eval import build_query_set, evaluate
from tests.conftest import insert_run, insert_job


def insert_sig(conn, job_id: int, text: str, key: str | None, source: str = "pytest_failed_summary"):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO failure_signatures
                (job_id, signature_source, exception_type, message_skeleton, top_frames, test_nodeids,
                 signature_text, signature_hash, retrieval_text, failure_key)
            VALUES (%s, %s, 'E', %s, '[]', '[]', %s, %s, %s, %s)
            """,
            (job_id, source, text, text, f"h{job_id}", text, key),
        )
    conn.commit()


@pytest.fixture()
def corpus(conn, repo_id):
    insert_run(conn, repo_id, run_id=1)
    for jid in (101, 102, 103, 104, 105):
        insert_job(conn, repo_id, job_id=jid, run_id=1)
    insert_sig(conn, 101, "AssertionError: Tensor-likes are not close! Mismatched elements: 3 / 100", "test:a")
    insert_sig(conn, 102, "TimeoutError: test took longer than <DUR> to complete", "test:b")
    insert_sig(conn, 103, "AssertionError: Tensor-likes are not close! Mismatched elements: 3 / 100", "test:a")  # exact dup of 101
    insert_sig(conn, 104, "AssertionError: Tensor-likes are not close! Mismatched elements: 7 / 100", "test:a")  # near dup
    insert_sig(conn, 105, "Error: Process completed with exit code 137", None, source="tail_fallback")
    return repo_id


def test_query_set_only_includes_keys_with_prior_occurrence(conn, corpus):
    qs = build_query_set(conn)
    ids = {q.job_id: q for q in qs}
    assert set(ids) == {103, 104}          # 101/102: no prior; 105: no key
    assert ids[103].relevant == {101}
    assert ids[104].relevant == {101, 103}
    assert ids[103].exact_dup is True
    assert ids[104].exact_dup is False


@pytest.mark.parametrize("retriever", [FullTextRetriever(), TrigramRetriever()])
def test_retrievers_find_prior_same_failure_first(conn, corpus, retriever):
    qs = build_query_set(conn)
    rep, per = evaluate(conn, retriever, qs, k=5)
    assert rep.n_queries == 2
    assert rep.mrr == 1.0
    assert rep.recall_at_k == 1.0


def test_retriever_never_returns_query_itself_or_later_jobs(conn, corpus):
    r = TrigramRetriever()
    ranked = r.search(conn, corpus, before_job_id=103, query_text="Tensor-likes are not close!", k=10)
    assert 103 not in ranked and 104 not in ranked
    assert ranked[0] == 101


def test_retriever_is_scoped_to_repo(conn, corpus):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO repos (owner, name, default_branch) VALUES ('other', 'repo', 'main') RETURNING id")
        other = cur.fetchone()[0]
    conn.commit()
    insert_run(conn, other, run_id=2)
    insert_job(conn, other, job_id=201, run_id=2)
    insert_sig(conn, 201, "AssertionError: Tensor-likes are not close! Mismatched elements: 3 / 100", "test:a")

    ranked = FullTextRetriever().search(conn, other, before_job_id=999, query_text="Tensor-likes are not close!", k=10)
    assert ranked == [201]
