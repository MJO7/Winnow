#!/usr/bin/env python3
"""Side-by-side of committed retrieval benchmarks, plus the per-query
head-to-head that characterizes WHERE the lexical baseline wins.

    python scripts/compare_retrieval.py --baseline fulltext_tsrank --challenger pgvector_hnsw --k 5 \
        --out docs/PHASE2_3_RETRIEVAL.md

Reads benchmarks/retrieval_<name>_k<k>.json; never re-runs anything.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent / "benchmarks"


def load(name: str, k: int) -> dict:
    return json.loads((BENCH_DIR / f"retrieval_{name}_k{k}.json").read_text())


def fmt(x) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default="fulltext_tsrank")
    ap.add_argument("--challenger", default="pgvector_hnsw")
    ap.add_argument("--also", default="trigram_similarity", help="extra retrievers for the summary table, comma-sep")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    names = [args.baseline] + [n for n in args.also.split(",") if n] + [args.challenger]
    reports = {n: load(n, args.k) for n in names}
    k = args.k
    L: list[str] = []

    L += [f"# Retrieval: lexical baseline vs pgvector (k={k})", ""]
    n_q = reports[args.baseline]["report"]["n_queries"]
    s = reports[args.baseline]["report"]["by_stratum"]
    L += [
        f"Query set: **{n_q}** failures with at least one prior same-`failure_key` occurrence in the same repo "
        f"({s['exact_dup']['n']} with an exact-text prior duplicate, {s['no_exact_dup']['n']} without). "
        "Query text is exception type + message skeleton; the test name is never in the query.",
        "",
        "## Overall",
        "",
        f"| Retriever | recall@{k} | MRR | hit@{k} | p50 ms | p95 ms |",
        "|---|---|---|---|---|---|",
    ]
    for n in names:
        r = reports[n]["report"]
        L.append(f"| `{n}` | {fmt(r['recall_at_k'])} | {fmt(r['mrr'])} | {fmt(r['hit_at_k'])} | {r['latency_p50_ms']} | {r['latency_p95_ms']} |")

    L += ["", "## By stratum (MRR)", "", "| Retriever | exact-dup prior exists | no exact-dup prior |", "|---|---|---|"]
    for n in names:
        st = reports[n]["report"]["by_stratum"]
        L.append(f"| `{n}` | {fmt(st['exact_dup'].get('mrr'))} (n={st['exact_dup']['n']}) | {fmt(st['no_exact_dup'].get('mrr'))} (n={st['no_exact_dup']['n']}) |")

    L += ["", "## By repo (MRR)", "", "| Repo | " + " | ".join(f"`{n}`" for n in names) + " |", "|---|" + "---|" * len(names)]
    repos = sorted(reports[args.baseline]["report"]["by_repo"])
    for repo in repos:
        L.append(f"| {repo} | " + " | ".join(fmt(reports[n]["report"]["by_repo"].get(repo, {}).get("mrr")) for n in names) + " |")

    # ---- head to head, per query -------------------------------------
    base = {q["job_id"]: q for q in reports[args.baseline]["per_query"]}
    chal = {q["job_id"]: q for q in reports[args.challenger]["per_query"]}
    common = sorted(set(base) & set(chal))
    wins = defaultdict(lambda: {"baseline": 0, "challenger": 0, "tie": 0})
    by_exc = defaultdict(lambda: {"baseline": 0, "challenger": 0, "tie": 0, "base_rr": 0.0, "chal_rr": 0.0, "n": 0})
    examples = {"baseline": [], "challenger": []}
    for jid in common:
        b, c = base[jid], chal[jid]
        stratum = "exact_dup" if b["exact_dup"] else "no_exact_dup"
        if b["rr"] > c["rr"]:
            who = "baseline"
        elif c["rr"] > b["rr"]:
            who = "challenger"
        else:
            who = "tie"
        wins[stratum][who] += 1
        e = by_exc[b["exception_type"] or "(none)"]
        e[who] += 1
        e["n"] += 1
        e["base_rr"] += b["rr"]
        e["chal_rr"] += c["rr"]
        if who != "tie" and len(examples[who]) < 5:
            examples[who].append((jid, b["repo"], b["exception_type"], b["rr"], c["rr"]))

    L += [
        "", f"## Head to head: `{args.baseline}` vs `{args.challenger}` (per-query reciprocal rank)", "",
        "| Stratum | baseline wins | challenger wins | tie |", "|---|---|---|---|",
    ]
    for stratum in ("exact_dup", "no_exact_dup"):
        w = wins[stratum]
        L.append(f"| {stratum} | {w['baseline']} | {w['challenger']} | {w['tie']} |")

    L += ["", "### By exception type (n >= 5), sorted by where the baseline's edge is largest", "",
          "| Exception type | n | baseline MRR | challenger MRR | baseline wins | challenger wins |", "|---|---|---|---|---|---|"]
    rows = [(k_, v) for k_, v in by_exc.items() if v["n"] >= 5]
    rows.sort(key=lambda kv: (kv[1]["base_rr"] - kv[1]["chal_rr"]) / kv[1]["n"], reverse=True)
    for exc, v in rows:
        L.append(f"| `{exc}` | {v['n']} | {v['base_rr'] / v['n']:.3f} | {v['chal_rr'] / v['n']:.3f} | {v['baseline']} | {v['challenger']} |")

    L += ["", "### Example queries", ""]
    for who in ("baseline", "challenger"):
        L.append(f"**{who} wins** (job_id, repo, exception, baseline RR -> challenger RR):")
        L.append("")
        for jid, repo, exc, br, cr in examples[who]:
            L.append(f"- {jid} · {repo} · `{exc}` · {br:.2f} -> {cr:.2f}")
        L.append("")

    text = "\n".join(L)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
