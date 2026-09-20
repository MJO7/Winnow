"""Retrieval evaluation: recall@k and MRR against known recurrences.

Query set: every signature with a failure_key that has at least one
PRIOR job in the same repo with the same failure_key. The relevant set
is those prior jobs. A query is stratified as `exact_dup` if any relevant
job has byte-identical retrieval_text -- that is the class where a
lexical baseline is expected to be unbeatable, and separating it out is
what lets the embedding's contribution (if any) be characterized rather
than averaged away.

recall@k uses denominator min(|relevant|, k), so a perfect retriever
scores 1.0 even when there are more than k relevant prior failures.
"""
from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psycopg

from winnow.retrieval import Retriever


@dataclass
class Query:
    job_id: int
    repo_id: int
    repo: str
    source: str
    exception_type: str | None
    failure_key: str
    text: str
    relevant: set[int]
    exact_dup: bool


@dataclass
class QueryResult:
    job_id: int
    repo: str
    source: str
    exception_type: str | None
    exact_dup: bool
    n_relevant: int
    first_hit_rank: int | None
    recall_at_k: float
    rr: float
    latency_ms: float


@dataclass
class EvalReport:
    retriever: str
    k: int
    n_queries: int
    recall_at_k: float
    mrr: float
    hit_at_k: float
    latency_p50_ms: float
    latency_p95_ms: float
    by_stratum: dict[str, dict] = field(default_factory=dict)
    by_repo: dict[str, dict] = field(default_factory=dict)
    by_source: dict[str, dict] = field(default_factory=dict)


def build_query_set(conn: psycopg.Connection, min_text_len: int = 8) -> list[Query]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fs.job_id, j.repo_id, r.owner || '/' || r.name, fs.signature_source,
                   fs.exception_type, fs.failure_key, fs.retrieval_text
            FROM failure_signatures fs
            JOIN jobs j ON j.job_id = fs.job_id
            JOIN repos r ON r.id = j.repo_id
            WHERE fs.failure_key IS NOT NULL AND length(fs.retrieval_text) >= %s
            ORDER BY fs.job_id
            """,
            (min_text_len,),
        )
        rows = cur.fetchall()

    # (repo_id, failure_key) -> list of (job_id, text) in job_id order
    groups: dict[tuple[int, str], list[tuple[int, str]]] = {}
    for job_id, repo_id, _, _, _, key, text in rows:
        groups.setdefault((repo_id, key), []).append((job_id, text))

    queries: list[Query] = []
    for job_id, repo_id, repo, source, exc, key, text in rows:
        prior = [(jid, t) for jid, t in groups[(repo_id, key)] if jid < job_id]
        if not prior:
            continue
        queries.append(
            Query(
                job_id=job_id, repo_id=repo_id, repo=repo, source=source, exception_type=exc,
                failure_key=key, text=text, relevant={jid for jid, _ in prior},
                exact_dup=any(t == text for _, t in prior),
            )
        )
    return queries


def evaluate(conn: psycopg.Connection, retriever: Retriever, queries: list[Query], k: int = 5) -> tuple[EvalReport, list[QueryResult]]:
    results: list[QueryResult] = []
    for q in queries:
        t0 = time.perf_counter()
        ranked = retriever.search(conn, q.repo_id, q.job_id, q.text, k)
        latency_ms = (time.perf_counter() - t0) * 1000
        first = next((i + 1 for i, jid in enumerate(ranked) if jid in q.relevant), None)
        hits = sum(1 for jid in ranked[:k] if jid in q.relevant)
        results.append(
            QueryResult(
                job_id=q.job_id, repo=q.repo, source=q.source, exception_type=q.exception_type,
                exact_dup=q.exact_dup, n_relevant=len(q.relevant), first_hit_rank=first,
                recall_at_k=hits / min(len(q.relevant), k), rr=(1.0 / first) if first else 0.0,
                latency_ms=latency_ms,
            )
        )
    return _aggregate(retriever.name, k, results), results


def _summ(rs: list[QueryResult]) -> dict:
    if not rs:
        return {"n": 0}
    return {
        "n": len(rs),
        "recall_at_k": round(statistics.mean(r.recall_at_k for r in rs), 4),
        "mrr": round(statistics.mean(r.rr for r in rs), 4),
        "hit_at_k": round(sum(1 for r in rs if r.first_hit_rank) / len(rs), 4),
    }


def _aggregate(name: str, k: int, rs: list[QueryResult]) -> EvalReport:
    lat = sorted(r.latency_ms for r in rs) or [0.0]
    top = _summ(rs)
    rep = EvalReport(
        retriever=name, k=k, n_queries=len(rs),
        recall_at_k=top.get("recall_at_k", 0.0), mrr=top.get("mrr", 0.0), hit_at_k=top.get("hit_at_k", 0.0),
        latency_p50_ms=round(lat[len(lat) // 2], 2), latency_p95_ms=round(lat[int(len(lat) * 0.95) - 1 if len(lat) > 1 else 0], 2),
    )
    rep.by_stratum = {
        "exact_dup": _summ([r for r in rs if r.exact_dup]),
        "no_exact_dup": _summ([r for r in rs if not r.exact_dup]),
    }
    for key in sorted({r.repo for r in rs}):
        rep.by_repo[key] = _summ([r for r in rs if r.repo == key])
    for key in sorted({r.source for r in rs}):
        rep.by_source[key] = _summ([r for r in rs if r.source == key])
    return rep


def write_report(report: EvalReport, results: list[QueryResult], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"retrieval_{report.retriever}_k{report.k}.json"
    path.write_text(json.dumps({"report": asdict(report), "per_query": [asdict(r) for r in results]}, indent=1))
    return path
