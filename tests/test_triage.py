from __future__ import annotations

from winnow.retrieval import TrigramRetriever
from winnow.triage import ModelCall, TriageVerdict, cached_call, cost_usd, prompt_hash, render_user_prompt, load_neighbors, SYSTEM_PROMPT
from winnow.triage_eval import (
    TriageRecord,
    apply_threshold,
    build_eval_set,
    choose_tau,
    confusion,
    held_out_predictions,
    run_eval,
    summarize,
)
from tests.conftest import insert_run, insert_job
from tests.test_retrieval import insert_sig


class FakeModel:
    """Returns a scripted verdict; counts real calls so cache hits are observable."""

    def __init__(self, verdicts: dict[str, TriageVerdict] | None = None, default: TriageVerdict | None = None, model: str = "claude-haiku-4-5"):
        self.model = model
        self.verdicts = verdicts or {}
        self.default = default or TriageVerdict(classification="flake", confidence=0.9, rationale="r", cited_run_id=None)
        self.calls = 0

    def call(self, system, user):
        self.calls += 1
        v = self.default
        for needle, verdict in self.verdicts.items():
            if needle in user:
                v = verdict
        return ModelCall(v, input_tokens=1000, output_tokens=50, cache_read_tokens=0, latency_ms=120.0, cached=False)


def _label(conn, job_id, run_id, repo_id, label, attempts=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO labels (job_id, run_id, repo_id, label, label_reason, attempts_to_resolution) VALUES (%s,%s,%s,%s,'t',%s)",
            (job_id, run_id, repo_id, label, attempts),
        )
    conn.commit()


def test_prompt_renders_neighbor_outcomes_and_run_ids(conn, repo_id):
    insert_run(conn, repo_id, run_id=1)
    insert_job(conn, repo_id, job_id=101, run_id=1)
    insert_sig(conn, 101, "TimeoutError: waited <DUR>", "test:a")
    _label(conn, 101, 1, repo_id, "flake", attempts=2)
    n = load_neighbors(conn, [101])
    text = render_user_prompt("TimeoutError: waited <DUR>", ["t.py::test_a"], n)
    assert "run_id=1" in text
    assert "passed on re-run" in text
    assert "K=1" in text


def test_cached_call_hits_cache_on_identical_prompt(conn):
    m = FakeModel()
    a = cached_call(conn, m, SYSTEM_PROMPT, "prompt A")
    b = cached_call(conn, m, SYSTEM_PROMPT, "prompt A")
    c = cached_call(conn, m, SYSTEM_PROMPT, "prompt B")
    assert m.calls == 2
    assert a.cached is False and b.cached is True and c.cached is False
    assert b.verdict == a.verdict and b.latency_ms == a.latency_ms
    assert prompt_hash(m.model, SYSTEM_PROMPT, "prompt A") != prompt_hash("claude-opus-5", SYSTEM_PROMPT, "prompt A")


def test_cost_uses_list_price_and_cache_discount():
    assert abs(cost_usd("claude-haiku-4-5", 1_000_000, 0) - 1.00) < 1e-9
    assert abs(cost_usd("claude-opus-5", 0, 1_000_000) - 25.00) < 1e-9
    assert abs(cost_usd("claude-opus-5", 0, 0, cache_read_tokens=1_000_000) - 0.50) < 1e-9


def test_eval_resolves_citations_against_shown_neighbors(conn, repo_id):
    insert_run(conn, repo_id, run_id=1)
    insert_run(conn, repo_id, run_id=2)
    insert_run(conn, repo_id, run_id=3)
    insert_job(conn, repo_id, job_id=101, run_id=1)
    insert_job(conn, repo_id, job_id=201, run_id=2)
    insert_job(conn, repo_id, job_id=301, run_id=3)
    insert_sig(conn, 101, "TimeoutError: waited <DUR> for the widget", "test:a")
    insert_sig(conn, 201, "KeyError: 'unrelated'", "test:b")
    insert_sig(conn, 301, "TimeoutError: waited <DUR> for the widget", "test:a")
    _label(conn, 101, 1, repo_id, "flake", attempts=2)
    _label(conn, 301, 3, repo_id, "flake")

    queries = build_eval_set(conn)
    assert [q.job_id for q in queries] == [101, 301]

    # 101 has no prior -> model cites nothing; 301's prior is 101 (run 1)
    model = FakeModel(verdicts={
        "K=0": TriageVerdict(classification="real", confidence=0.4, rationale="no history", cited_run_id=None),
        "run_id=1": TriageVerdict(classification="flake", confidence=0.95, rationale="run 1 passed on re-run", cited_run_id=1),
    })
    records = run_eval(conn, model, TrigramRetriever(), queries, k=3)
    by_id = {r.job_id: r for r in records}
    assert by_id[101].citation == "none"
    assert by_id[301].citation == "supported_same_key"
    assert by_id[301].cost_usd > 0

    # an invented run id resolves as hallucinated; an existing-but-not-shown one as unsupported.
    # Different model names: the cache is keyed by (model, prompt) and would otherwise (correctly) replay.
    model2 = FakeModel(default=TriageVerdict(classification="flake", confidence=0.9, rationale="x", cited_run_id=999999), model="claude-sonnet-5")
    rec = run_eval(conn, model2, TrigramRetriever(), queries[1:], k=3)[0]
    assert rec.citation == "hallucinated"
    model3 = FakeModel(default=TriageVerdict(classification="flake", confidence=0.9, rationale="x", cited_run_id=2), model="claude-opus-5")
    rec = run_eval(conn, model3, TrigramRetriever(), queries[1:], k=1)[0]
    assert rec.citation == "unsupported_exists"


def _rec(repo, gold, pred, conf):
    return TriageRecord(job_id=0, repo=repo, gold=gold, predicted=pred, confidence=conf, cited_run_id=None, citation="none",
                        n_neighbors=0, retrieval_ms=1, generation_ms=1, input_tokens=0, output_tokens=0, cache_read_tokens=0,
                        cost_usd=0, cached=False, rationale="")


def test_threshold_escalates_low_confidence_flakes_and_is_chosen_on_other_repos():
    recs = [
        _rec("A", "real", "flake", 0.55),   # missed regression at low confidence
        _rec("A", "flake", "flake", 0.95),
        _rec("A", "flake", "flake", 0.60),  # escalating this costs 1
        _rec("B", "real", "flake", 0.50),
        _rec("B", "flake", "flake", 0.97),
    ]
    assert apply_threshold(recs, 0.0) == [r.predicted for r in recs]
    tau = choose_tau(recs)
    assert 0.55 < tau <= 0.95           # escalates both missed regressions, keeps the .95/.97 flakes
    preds, taus = held_out_predictions(recs)
    assert set(taus) == {"A", "B"}
    m = confusion(recs, preds)
    assert m["real"]["flake"] == 0

    rep = summarize("claude-haiku-4-5", "trigram_similarity", 3, recs)
    assert rep.heldout_missed_regressions == 0
    assert rep.n == 5
