#!/usr/bin/env python3
"""Run the triage eval for a grid of (model x retriever x k).

    python scripts/eval_triage.py --dry-run                       # size + cost estimate, no API calls
    python scripts/eval_triage.py --models claude-haiku-4-5 --retrievers fulltext --ks 3
    python scripts/eval_triage.py --sweep                         # 2 models x 2 retrievers x 2 ks

Every model response is cached by prompt hash (llm_cache); a rerun of
any configuration costs $0 and is byte-identical. Requires
ANTHROPIC_API_KEY for uncached calls.
"""
import argparse
import itertools
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from winnow.db import get_conn
from winnow.retrieval import FullTextRetriever, TrigramRetriever
from winnow.triage import PRICING, SYSTEM_PROMPT, AnthropicTriageClient, load_neighbors, render_user_prompt, prompt_hash
from winnow.triage_eval import build_eval_set, run_eval, summarize, write_report

BENCH_DIR = Path(__file__).resolve().parent.parent / "benchmarks"

RETRIEVERS = {"fulltext": FullTextRetriever, "trigram": TrigramRetriever}


def _vector():
    from winnow.vector import VectorRetriever
    return VectorRetriever()


def make_retriever(name: str):
    if name == "vector":
        return _vector()
    return RETRIEVERS[name]()


def estimate(conn, queries, retriever_name, k, model) -> tuple[int, float, int]:
    """Chars/4 token estimate for uncached prompts; exact for cached ones."""
    r = make_retriever(retriever_name)
    est_in, uncached = 0, 0
    with conn.cursor() as cur:
        for q in queries:
            user = render_user_prompt(q.text, q.nodeids, load_neighbors(conn, r.search(conn, q.repo_id, q.job_id, q.text, k)))
            cur.execute("SELECT 1 FROM llm_cache WHERE prompt_hash = %s", (prompt_hash(model, SYSTEM_PROMPT, user),))
            if cur.fetchone():
                continue
            uncached += 1
            est_in += (len(SYSTEM_PROMPT) + len(user)) // 4
    inp, out = PRICING[model]
    est_cost = (est_in * inp + uncached * 150 * out) / 1e6
    return uncached, est_cost, est_in


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="claude-haiku-4-5,claude-opus-5")
    ap.add_argument("--retrievers", default="fulltext,vector")
    ap.add_argument("--ks", default="3,10")
    ap.add_argument("--per-repo", type=int, default=None, help="sample this many labeled jobs per repo (default: all)")
    ap.add_argument("--effort", default="low", help="output_config.effort for thinking-capable models")
    ap.add_argument("--sweep", action="store_true", help="run the full grid (default grid = the spec's minimum sweep)")
    ap.add_argument("--dry-run", action="store_true", help="estimate uncached calls and cost; make no API calls")
    args = ap.parse_args()

    models = args.models.split(",")
    retrievers = args.retrievers.split(",")
    ks = [int(k) for k in args.ks.split(",")]
    grid = list(itertools.product(models, retrievers, ks)) if (args.sweep or args.dry_run) else [(models[0], retrievers[0], ks[0])]

    with get_conn() as conn:
        queries = build_eval_set(conn, per_repo=args.per_repo)
        by_repo = {}
        for q in queries:
            by_repo[q.repo] = by_repo.get(q.repo, 0) + 1
        golds = {}
        for q in queries:
            golds[q.gold] = golds.get(q.gold, 0) + 1
        print(f"eval set: {len(queries)} labeled failures with signatures  by_repo={by_repo}  gold={golds}")

        if args.dry_run:
            total = 0.0
            print(f"\n{'model':<18} {'retriever':<10} {'k':>3} {'uncached':>9} {'est_in_tok':>11} {'est_$':>8}")
            for model, rname, k in grid:
                n, cost, tok = estimate(conn, queries, rname, k, model)
                total += cost
                print(f"{model:<18} {rname:<10} {k:>3} {n:>9} {tok:>11} {cost:>8.2f}")
            print(f"\nestimated total for uncached calls: ${total:.2f} (list price, chars/4 tokens, ~150 output tokens each)")
            return

        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is not set; only cached calls will succeed. Set it in .env or the environment.")

        for model, rname, k in grid:
            print(f"\n== {model} / {rname} / k={k}")
            client = AnthropicTriageClient(model=model, effort=args.effort)
            records = run_eval(conn, client, make_retriever(rname), queries, k=k)
            rep = summarize(model, rname, k, records)
            path = write_report(rep, records, BENCH_DIR)
            print(
                f"   raw macroF1={rep.raw_macro_f1:.3f} missed_regressions={rep.raw_missed_regressions} | "
                f"held-out macroF1={rep.heldout_macro_f1:.3f} missed={rep.heldout_missed_regressions} false_flakes={rep.heldout_false_flakes} | "
                f"hallucinated={rep.citation_rates['hallucinated']:.3f} unsupported={rep.citation_rates['unsupported_exists']:.3f} | "
                f"${rep.cost_per_1k_usd:.2f}/1k  e2e p95={rep.latency['e2e_p95_ms']}ms  cached={rep.cached_fraction:.0%}"
            )
            print(f"   -> {path.relative_to(BENCH_DIR.parent)}")


if __name__ == "__main__":
    main()
