"""Triage evaluation (Phase 4).

Eval set: every labeled job that has a parsed signature. For each, the
retriever supplies K prior failures from the same repo, the model returns
a verdict, and four things are measured:

  1. classification vs the derived label, as a 3x3 confusion matrix plus
     the asymmetric error that matters: real -> flake (a missed regression)
  2. citation resolution: does cited_run_id exist, and was it one of the
     neighbors actually shown (support), and does that neighbor share the
     query's failure_key (strong support)
  3. cost, from actual usage tokens at list price
  4. latency, retrieval and generation attributed separately

Held out by repository: the confidence threshold that decides when a
'flake' verdict is trusted vs escalated to 'real' is chosen on the other
repos and applied to the held-out one. An LLM isn't trained here, so this
is the only free parameter that could overfit -- and it is the one that
sets the operating point.
"""
from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psycopg

from winnow.retrieval import Retriever
from winnow.triage import (
    SYSTEM_PROMPT,
    ModelClient,
    cached_call,
    cost_usd,
    load_neighbors,
    render_user_prompt,
)

CLASSES = ("flake", "real", "infra")
MISSED_REGRESSION_WEIGHT = 10.0   # real->flake costs 10x a flake->real escalation


@dataclass
class TriageQuery:
    job_id: int
    repo_id: int
    repo: str
    gold: str
    failure_key: str | None
    text: str
    nodeids: list[str]


@dataclass
class TriageRecord:
    job_id: int
    repo: str
    gold: str
    predicted: str
    confidence: float
    cited_run_id: int | None
    citation: str                 # supported_same_key | supported | unsupported_exists | hallucinated | none
    n_neighbors: int
    retrieval_ms: float
    generation_ms: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float
    cached: bool
    rationale: str


def build_eval_set(conn: psycopg.Connection, per_repo: int | None = None, seed: int = 7) -> list[TriageQuery]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.job_id, l.repo_id, r.owner || '/' || r.name, l.label, fs.failure_key, fs.retrieval_text, fs.test_nodeids
            FROM labels l
            JOIN failure_signatures fs ON fs.job_id = l.job_id
            JOIN repos r ON r.id = l.repo_id
            WHERE fs.retrieval_text IS NOT NULL
            ORDER BY l.repo_id, l.job_id
            """
        )
        rows = cur.fetchall()
    queries = [TriageQuery(*row[:6], list(row[6] or [])) for row in rows]
    if per_repo is None:
        return queries
    import random

    rng = random.Random(seed)
    out: list[TriageQuery] = []
    for repo in sorted({q.repo for q in queries}):
        group = [q for q in queries if q.repo == repo]
        rng.shuffle(group)
        out.extend(sorted(group[:per_repo], key=lambda q: q.job_id))
    return out


def _resolve_citation(conn, cited: int | None, neighbor_job_ids: list[int], query_key: str | None) -> str:
    if cited is None:
        return "none"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT j.job_id, fs.failure_key FROM jobs j LEFT JOIN failure_signatures fs ON fs.job_id = j.job_id WHERE j.run_id = %s",
            (cited,),
        )
        rows = cur.fetchall()
    if not rows:
        return "hallucinated"
    shown = [(jid, key) for jid, key in rows if jid in neighbor_job_ids]
    if not shown:
        return "unsupported_exists"
    if query_key and any(key == query_key for _, key in shown):
        return "supported_same_key"
    return "supported"


def run_eval(
    conn: psycopg.Connection,
    client: ModelClient,
    retriever: Retriever,
    queries: list[TriageQuery],
    k: int,
    progress_every: int = 25,
) -> list[TriageRecord]:
    records: list[TriageRecord] = []
    for i, q in enumerate(queries, 1):
        t0 = time.perf_counter()
        neighbor_ids = retriever.search(conn, q.repo_id, q.job_id, q.text, k)
        neighbors = load_neighbors(conn, neighbor_ids)
        retrieval_ms = (time.perf_counter() - t0) * 1000

        user = render_user_prompt(q.text, q.nodeids, neighbors)
        call = cached_call(conn, client, SYSTEM_PROMPT, user)
        v = call.verdict
        records.append(
            TriageRecord(
                job_id=q.job_id, repo=q.repo, gold=q.gold, predicted=v.classification, confidence=v.confidence,
                cited_run_id=v.cited_run_id,
                citation=_resolve_citation(conn, v.cited_run_id, [n.job_id for n in neighbors], q.failure_key),
                n_neighbors=len(neighbors), retrieval_ms=retrieval_ms, generation_ms=call.latency_ms,
                input_tokens=call.input_tokens, output_tokens=call.output_tokens, cache_read_tokens=call.cache_read_tokens,
                cost_usd=cost_usd(client.model, call.input_tokens, call.output_tokens, call.cache_read_tokens),
                cached=call.cached, rationale=v.rationale,
            )
        )
        if i % progress_every == 0:
            print(f"  {i}/{len(queries)} ({sum(r.cached for r in records)} cached)")
    return records


# ---- metrics ------------------------------------------------------------

def apply_threshold(records: list[TriageRecord], tau: float) -> list[str]:
    """A 'flake' verdict below confidence tau is escalated to 'real'.
    Escalation is the safe direction: the cost of a human looking at a
    flake is minutes; the cost of ignoring a regression is a broken main."""
    return [("real" if r.predicted == "flake" and r.confidence < tau else r.predicted) for r in records]


def confusion(records: list[TriageRecord], preds: list[str]) -> dict[str, dict[str, int]]:
    m = {g: {p: 0 for p in CLASSES} for g in CLASSES}
    for r, p in zip(records, preds):
        m[r.gold][p] += 1
    return m


def prf(m: dict[str, dict[str, int]]) -> dict[str, dict[str, float]]:
    out = {}
    for c in CLASSES:
        tp = m[c][c]
        fp = sum(m[g][c] for g in CLASSES if g != c)
        fn = sum(m[c][p] for p in CLASSES if p != c)
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f = 2 * p * r / (p + r) if p + r else 0.0
        out[c] = {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4), "support": tp + fn}
    return out


def weighted_cost(m: dict[str, dict[str, int]]) -> float:
    return MISSED_REGRESSION_WEIGHT * m["real"]["flake"] + m["flake"]["real"]


def choose_tau(records: list[TriageRecord]) -> float:
    best, best_cost = 0.0, float("inf")
    for tau in [i / 20 for i in range(0, 21)]:
        c = weighted_cost(confusion(records, apply_threshold(records, tau)))
        if c <= best_cost:  # ties go to the larger tau: the more conservative choice
            best, best_cost = tau, c
    return best


def held_out_predictions(records: list[TriageRecord]) -> tuple[list[str], dict[str, float]]:
    """Leave-one-repo-out: tau for repo R is chosen on all records not in R."""
    taus: dict[str, float] = {}
    preds: list[str] = []
    for repo in sorted({r.repo for r in records}):
        taus[repo] = choose_tau([r for r in records if r.repo != repo])
    for r in records:
        preds.append("real" if r.predicted == "flake" and r.confidence < taus[r.repo] else r.predicted)
    return preds, taus


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return round(s[min(len(s) - 1, int(len(s) * p))], 1)


@dataclass
class TriageReport:
    model: str
    retriever: str
    k: int
    n: int
    raw_confusion: dict
    raw_prf: dict
    raw_macro_f1: float
    raw_missed_regressions: int
    heldout_taus: dict
    heldout_confusion: dict
    heldout_prf: dict
    heldout_macro_f1: float
    heldout_missed_regressions: int
    heldout_false_flakes: int
    citation_rates: dict
    cost_per_1k_usd: float
    cost_breakdown_per_1k: dict
    latency: dict
    by_repo_heldout: dict = field(default_factory=dict)
    cached_fraction: float = 0.0


def summarize(model: str, retriever: str, k: int, records: list[TriageRecord]) -> TriageReport:
    n = len(records)
    raw_preds = [r.predicted for r in records]
    raw_m = confusion(records, raw_preds)
    raw_p = prf(raw_m)

    ho_preds, taus = held_out_predictions(records)
    ho_m = confusion(records, ho_preds)
    ho_p = prf(ho_m)

    cits = {c: 0 for c in ("supported_same_key", "supported", "unsupported_exists", "hallucinated", "none")}
    for r in records:
        cits[r.citation] += 1
    cit_rates = {c: round(v / n, 4) for c, v in cits.items()} if n else cits

    from winnow.triage import PRICING
    inp_rate, out_rate = PRICING[model]
    in_cost = sum(r.input_tokens * inp_rate + r.cache_read_tokens * inp_rate * 0.1 for r in records) / 1e6
    out_cost = sum(r.output_tokens * out_rate for r in records) / 1e6
    total = in_cost + out_cost

    by_repo = {}
    for repo in sorted({r.repo for r in records}):
        idx = [i for i, r in enumerate(records) if r.repo == repo]
        sub = [records[i] for i in idx]
        m = confusion(sub, [ho_preds[i] for i in idx])
        p = prf(m)
        by_repo[repo] = {
            "n": len(sub), "tau": taus[repo],
            "macro_f1": round(statistics.mean(p[c]["f1"] for c in CLASSES if p[c]["support"]), 4) if any(p[c]["support"] for c in CLASSES) else 0.0,
            "missed_regressions": m["real"]["flake"], "false_flakes": m["flake"]["real"],
        }

    e2e = [r.retrieval_ms + r.generation_ms for r in records]
    return TriageReport(
        model=model, retriever=retriever, k=k, n=n,
        raw_confusion=raw_m, raw_prf=raw_p,
        raw_macro_f1=round(statistics.mean(raw_p[c]["f1"] for c in CLASSES if raw_p[c]["support"]), 4) if n else 0.0,
        raw_missed_regressions=raw_m["real"]["flake"],
        heldout_taus=taus, heldout_confusion=ho_m, heldout_prf=ho_p,
        heldout_macro_f1=round(statistics.mean(ho_p[c]["f1"] for c in CLASSES if ho_p[c]["support"]), 4) if n else 0.0,
        heldout_missed_regressions=ho_m["real"]["flake"], heldout_false_flakes=ho_m["flake"]["real"],
        citation_rates=cit_rates,
        cost_per_1k_usd=round(total / n * 1000, 4) if n else 0.0,
        cost_breakdown_per_1k={"input_usd": round(in_cost / n * 1000, 4) if n else 0.0,
                               "output_usd": round(out_cost / n * 1000, 4) if n else 0.0,
                               "embedding_usd": 0.0,
                               "note": "embeddings are a local model: $0 marginal, wall-clock only"},
        latency={
            "retrieval_p50_ms": _pct([r.retrieval_ms for r in records], 0.5),
            "retrieval_p95_ms": _pct([r.retrieval_ms for r in records], 0.95),
            "generation_p50_ms": _pct([r.generation_ms for r in records], 0.5),
            "generation_p95_ms": _pct([r.generation_ms for r in records], 0.95),
            "e2e_p50_ms": _pct(e2e, 0.5), "e2e_p95_ms": _pct(e2e, 0.95),
        },
        by_repo_heldout=by_repo,
        cached_fraction=round(sum(r.cached for r in records) / n, 4) if n else 0.0,
    )


def write_report(report: TriageReport, records: list[TriageRecord], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = report.model.replace("claude-", "")
    path = out_dir / f"triage_{slug}_{report.retriever}_k{report.k}.json"
    path.write_text(json.dumps({"report": asdict(report), "per_query": [asdict(r) for r in records]}, indent=1))
    return path
