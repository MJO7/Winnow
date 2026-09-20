"""Retrievers over failure_signatures.retrieval_text.

Every retriever answers the same question with the same constraints:
given a failing job, which PRIOR failed jobs in the SAME repo look like
it? Repo and recency filters are pushed into the SQL statement alongside
the ranking expression rather than applied after the fact in Python --
that is the whole reason the index lives in Postgres.
"""
from __future__ import annotations

from typing import Protocol

import psycopg


class Retriever(Protocol):
    name: str

    def search(self, conn: psycopg.Connection, repo_id: int, before_job_id: int, query_text: str, k: int) -> list[int]:
        """Returns up to k job_ids, best first."""


class FullTextRetriever:
    """tsvector / ts_rank. Terms are OR-ed, not AND-ed: two failures that
    share most of a message but differ in one token must still match."""

    name = "fulltext_tsrank"

    def search(self, conn, repo_id, before_job_id, query_text, k):
        with conn.cursor() as cur:
            cur.execute("SELECT tsvector_to_array(to_tsvector('simple', %s))", (query_text,))
            lexemes = cur.fetchone()[0]
            if not lexemes:
                return []
            q = " OR ".join('"' + lx.replace('"', "") + '"' for lx in lexemes)
            cur.execute(
                """
                SELECT fs.job_id
                FROM failure_signatures fs
                JOIN jobs j ON j.job_id = fs.job_id
                WHERE j.repo_id = %s AND fs.job_id < %s
                  AND fs.tsv @@ websearch_to_tsquery('simple', %s)
                ORDER BY ts_rank(fs.tsv, websearch_to_tsquery('simple', %s)) DESC, fs.job_id DESC
                LIMIT %s
                """,
                (repo_id, before_job_id, q, q, k),
            )
            return [r[0] for r in cur.fetchall()]


class TrigramRetriever:
    name = "trigram_similarity"

    def search(self, conn, repo_id, before_job_id, query_text, k):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT fs.job_id
                FROM failure_signatures fs
                JOIN jobs j ON j.job_id = fs.job_id
                WHERE j.repo_id = %s AND fs.job_id < %s AND fs.retrieval_text IS NOT NULL
                ORDER BY similarity(fs.retrieval_text, %s) DESC, fs.job_id DESC
                LIMIT %s
                """,
                (repo_id, before_job_id, query_text, k),
            )
            return [r[0] for r in cur.fetchall()]
