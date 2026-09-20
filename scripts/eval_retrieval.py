#!/usr/bin/env python3
"""Run the retrieval eval for one or more retrievers on the same query set.

    python scripts/eval_retrieval.py --retrievers fulltext,trigram --k 5

Writes benchmarks/retrieval_<name>_k<k>.json (aggregate + per-query) so
later retrievers are compared against a committed baseline, not a rerun.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from winnow.db import get_conn
from winnow.retrieval import FullTextRetriever, TrigramRetriever
from winnow.retrieval_eval import build_query_set, evaluate, write_report

BENCH_DIR = Path(__file__).resolve().parent.parent / "benchmarks"


def make_retrievers(names: list[str]):
    out = []
    for n in names:
        if n == "fulltext":
            out.append(FullTextRetriever())
        elif n == "trigram":
            out.append(TrigramRetriever())
        elif n == "vector":
            from winnow.vector import VectorRetriever
            out.append(VectorRetriever())
        else:
            raise SystemExit(f"unknown retriever {n!r}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--retrievers", default="fulltext,trigram")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    with get_conn() as conn:
        queries = build_query_set(conn)
        n_dup = sum(1 for q in queries if q.exact_dup)
        print(f"query set: {len(queries)} queries ({n_dup} with an exact-text prior duplicate, {len(queries) - n_dup} without)")
        if not queries:
            raise SystemExit("no queries: build signatures first (scripts/label_repo.py)")

        print(f"\n{'retriever':<22} {'n':>5} {'recall@k':>9} {'MRR':>7} {'hit@k':>7} {'p50ms':>7} {'p95ms':>7}   exact_dup MRR | no_dup MRR")
        for r in make_retrievers(args.retrievers.split(",")):
            rep, per_query = evaluate(conn, r, queries, k=args.k)
            path = write_report(rep, per_query, BENCH_DIR)
            s = rep.by_stratum
            print(
                f"{rep.retriever:<22} {rep.n_queries:>5} {rep.recall_at_k:>9.3f} {rep.mrr:>7.3f} {rep.hit_at_k:>7.3f} "
                f"{rep.latency_p50_ms:>7.1f} {rep.latency_p95_ms:>7.1f}   "
                f"{s['exact_dup'].get('mrr', float('nan')):.3f} (n={s['exact_dup']['n']}) | "
                f"{s['no_exact_dup'].get('mrr', float('nan')):.3f} (n={s['no_exact_dup']['n']})"
            )
            print(f"  -> {path.relative_to(BENCH_DIR.parent)}")


if __name__ == "__main__":
    main()
