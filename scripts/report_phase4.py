#!/usr/bin/env python3
"""Phase 4 report: the accuracy-vs-cost frontier plot, per-config table,
confusion matrices, citation rates, and latency -- all from the committed
benchmarks/triage_*.json files. Never calls a model.

    python scripts/report_phase4.py --out docs/PHASE4_TRIAGE.md --plot docs/phase4_frontier.png
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

BENCH_DIR = Path(__file__).resolve().parent.parent / "benchmarks"

# Validated 2-slot categorical palette (dataviz skill, light surface):
# blue <-> orange, CVD dE 24.7, normal-vision dE 33.6.
SERIES = {"claude-opus-5": "#2a78d6", "claude-sonnet-5": "#1baf7a", "claude-haiku-4-5": "#eb6834"}
MARKERS = {"fulltext_tsrank": "o", "pgvector_hnsw": "s", "trigram_similarity": "^"}
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e6e5e1", "#fcfcfb"


def load_all() -> list[dict]:
    out = []
    for p in sorted(BENCH_DIR.glob("triage_*.json")):
        out.append(json.loads(p.read_text())["report"])
    return out


def pareto(points: list[tuple[float, float]]) -> list[int]:
    """Indices on the upper-left frontier: no other point is both cheaper and more accurate."""
    idx = []
    for i, (c, a) in enumerate(points):
        dominated = any((c2 <= c and a2 >= a) and (c2 < c or a2 > a) for j, (c2, a2) in enumerate(points) if j != i)
        if not dominated:
            idx.append(i)
    return sorted(idx, key=lambda i: points[i][0])


def plot(reports: list[dict], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9.5, 5.2), dpi=160, facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    pts = [(r["cost_per_1k_usd"], r["heldout_macro_f1"]) for r in reports]
    front = pareto(pts)
    if len(front) >= 2:
        ax.plot([pts[i][0] for i in front], [pts[i][1] for i in front], color=INK2, lw=1.2, ls="--", zorder=1)

    seen_models = []
    for r, (c, a) in zip(reports, pts):
        color = SERIES.get(r["model"], INK2)
        ax.scatter(c, a, s=70, marker=MARKERS.get(r["retriever"], "o"), facecolor=color, edgecolor=SURFACE, linewidth=1.5, zorder=3)
        ax.annotate(f"k={r['k']}", (c, a), xytext=(6, 5), textcoords="offset points", fontsize=8, color=INK2)
        if r["model"] not in seen_models:
            seen_models.append(r["model"])

    ax.set_xscale("log")
    from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator
    lo, hi = min(c for c, _ in pts), max(c for c, _ in pts)
    ticks = [t for t in (0.1, 0.2, 0.3, 0.5, 1, 2, 3, 5, 10, 20, 30, 50, 100) if lo / 1.6 <= t <= hi * 1.6]
    ax.xaxis.set_major_locator(FixedLocator(ticks))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:g}"))
    ax.set_xlabel("Cost per 1,000 triages (USD, list price, log scale)", color=INK2, fontsize=9)
    ax.set_ylabel("Held-out macro F1 (flake / real / infra)", color=INK2, fontsize=9)
    ax.set_title("Triage accuracy vs cost", color=INK, fontsize=12, loc="left")
    ax.grid(True, color=GRID, lw=0.8, zorder=0)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=8)

    from matplotlib.lines import Line2D

    handles = [Line2D([], [], marker="o", ls="", color=SERIES.get(m, INK2), markersize=8, label=m) for m in seen_models]
    handles += [Line2D([], [], marker=MARKERS[n], ls="", color=INK2, markersize=7, label=n) for n in MARKERS if any(r["retriever"] == n for r in reports)]
    ax.legend(handles=handles, frameon=False, fontsize=8, labelcolor=INK2, loc="upper left", bbox_to_anchor=(1.01, 1.0))
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=SURFACE)


def cm_table(m: dict) -> list[str]:
    cls = ("flake", "real", "infra")
    return ["| gold \\ predicted | " + " | ".join(cls) + " |", "|---|---|---|---|"] + [
        f"| **{g}** | " + " | ".join(str(m[g][p]) for p in cls) + " |" for g in cls
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--plot", default=None)
    args = ap.parse_args()

    reports = load_all()
    if not reports:
        raise SystemExit("no benchmarks/triage_*.json yet -- run scripts/eval_triage.py")
    if args.plot:
        plot(reports, Path(args.plot))

    L = ["# Phase 4: triage evaluation", ""]
    n = reports[0]["n"]
    L += [f"Eval set: **{n}** derived-labeled failures with parsed signatures, across {len(reports[0]['by_repo_heldout'])} repositories. "
          "Operating point (the confidence below which a 'flake' verdict is escalated to 'real', at a 10:1 missed-regression cost) "
          "is chosen leave-one-repo-out. Every model response is served from the prompt-hash cache on rerun.", ""]
    if args.plot:
        L += [f"![accuracy vs cost]({Path(args.plot).name})", ""]

    L += ["## Configurations", "",
          "| Model | Retriever | k | raw macro F1 | held-out macro F1 | missed regressions (held-out) | false flakes | hallucinated cites | unsupported cites | $/1k | e2e p50 / p95 ms |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in reports:
        c = r["citation_rates"]
        lat = r["latency"]
        L.append(f"| {r['model']} | {r['retriever']} | {r['k']} | {r['raw_macro_f1']:.3f} | {r['heldout_macro_f1']:.3f} | "
                 f"{r['heldout_missed_regressions']} | {r['heldout_false_flakes']} | {c['hallucinated']:.1%} | {c['unsupported_exists']:.1%} | "
                 f"${r['cost_per_1k_usd']:.2f} | {lat['e2e_p50_ms']} / {lat['e2e_p95_ms']} |")

    pts = [(r["cost_per_1k_usd"], r["heldout_macro_f1"]) for r in reports]
    front = pareto(pts)
    L += ["", "**Frontier** (no configuration is both cheaper and more accurate): " +
          ", ".join(f"{reports[i]['model']}/{reports[i]['retriever']}/k={reports[i]['k']} (${pts[i][0]:.2f}, F1 {pts[i][1]:.3f})" for i in front), ""]

    for r in reports:
        L += [f"## {r['model']} / {r['retriever']} / k={r['k']}", "",
              "Held-out confusion matrix:", ""] + cm_table(r["heldout_confusion"]) + ["",
              "| Class | precision | recall | F1 | support |", "|---|---|---|---|---|"]
        for cls, v in r["heldout_prf"].items():
            L.append(f"| {cls} | {v['precision']:.3f} | {v['recall']:.3f} | {v['f1']:.3f} | {v['support']} |")
        c = r["citation_rates"]
        L += ["", "Citations: " + ", ".join(f"{k} {v:.1%}" for k, v in c.items()),
              f"Cost: ${r['cost_per_1k_usd']:.2f}/1k (input ${r['cost_breakdown_per_1k']['input_usd']:.2f}, output ${r['cost_breakdown_per_1k']['output_usd']:.2f}, embeddings $0 local)",
              f"Latency: retrieval p50/p95 {r['latency']['retrieval_p50_ms']}/{r['latency']['retrieval_p95_ms']} ms, "
              f"generation p50/p95 {r['latency']['generation_p50_ms']}/{r['latency']['generation_p95_ms']} ms",
              "", "| Repo (held out) | n | tau | macro F1 | missed regressions | false flakes |", "|---|---|---|---|---|---|"]
        for repo, v in r["by_repo_heldout"].items():
            L.append(f"| {repo} | {v['n']} | {v['tau']:.2f} | {v['macro_f1']:.3f} | {v['missed_regressions']} | {v['false_flakes']} |")
        L.append("")

    text = "\n".join(L)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
