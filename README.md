# Winnow

A CI failure triage service. Reads public GitHub Actions history and answers one question per failure: is this a flaky test or a real regression, and here is the prior occurrence that proves it.

**Status: Phases 0–4 built; first-corpus numbers in.** Local harness, ingest, signature normalizer, derived-label join, lexical and pgvector retrieval with evals, and the triage generation + evaluation harness are implemented and tested (48 tests). Numbers are from the first corpus (84k jobs, 1,325 failed-job logs) and are being regenerated on a 30-day windowed corpus.

| Result | Value | Where |
|---|---|---|
| Job records / failed-job logs | 84,092 / 1,325 (first corpus) | `docs/PHASE1_RESULTS.md` |
| Derived labels (flake / real / infra) | 14 / 95 / 71 | `docs/PHASE1_RESULTS.md` |
| Retrieval recall@5, lexical vs pgvector | 0.981 vs 0.985 (MRR 0.986 vs 0.989), 434 queries | `docs/PHASE2_3_RETRIEVAL.md` |
| Where the embedding helped | no-exact-duplicate stratum only: 0.928 → 0.947 MRR, n=71, 4 wins / 2 losses / 65 ties | `docs/PHASE2_3_RETRIEVAL.md` |
| Triage precision / recall / hallucination / $ per 1k | pending `ANTHROPIC_API_KEY` (~$6 for the full sweep on this set) | `docs/PHASE4_TRIAGE.md` |

## The idea

Ground truth derives itself from the data. A failure is **flaky** when the same job, at the same commit SHA, later succeeds on re-run with no code change. It is **real** when that job never succeeds at that SHA and a later commit — confirmed via the compare API to be a descendant, not a force-push rewrite — runs it green. It is **infra** when a GitHub-injected lifecycle step (`Set up job`, `Initialize containers`, …) failed rather than a test step. No hand-labeling; every label traces to a `run_id` you can open in a browser.

A derived label is still a proxy. Phase 1 measures it against an independent signal: pytorch's flaky-test bot, which auto-files `DISABLED test_x (module.Class)` issues. See `docs/adr/0001-derived-labels.md`.

## Corpus

Five repositories, all GitHub Actions native, all Python test suites (so the normalizer targets pytest/traceback shapes specifically rather than a multi-language grammar):

| Repo | Default branch | Why |
|---|---|---|
| pytorch/pytorch | main | External validation signal (flaky-test bot). Dozens of runs/hour. |
| home-assistant/core | dev | Large pytest suite, matrix across Python + service versions. |
| apache/airflow | main | Large matrix, known flaky-test history. |
| python/cpython | main | unittest, OS/arch matrix. |
| pandas-dev/pandas | main | pytest, OS + NumPy version matrix. |

Volume verified 2026-09-19 via the public API: every one returns dozens of runs inside a single hour (pytorch: ~9,500 push runs in two days). Log retention verified ≥31 days for pytorch and home-assistant by fetching 2026-08-19 job logs on 2026-09-19; a single pytorch job log was 2.0 MB, five times DynamoDB's item limit — which is why raw logs go to S3 and only signatures go to Dynamo (Phase 5).

## Setup

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env            # add GITHUB_TOKEN
docker compose up -d
python scripts/migrate.py
```

## Run

```bash
# Phase 0: runs sampled per day over a window, jobs, steps, failed-job logs
python scripts/ingest_repo.py --repo pytorch/pytorch --days 30 --per-day 15 --fetch-logs

# Phase 1: signatures, labels, exclusions, retry-config scan, external signal
python scripts/label_repo.py --repo pytorch/pytorch
python scripts/report_phase1.py --out docs/PHASE1_RESULTS.md

# Phase 2: lexical baseline (commit this before running Phase 3)
python scripts/eval_retrieval.py --retrievers fulltext,trigram --k 5

# Phase 3: embeddings (local, $0) + pgvector, then the side-by-side
python scripts/embed_signatures.py
python scripts/eval_retrieval.py --retrievers vector --k 5
python scripts/compare_retrieval.py --out docs/PHASE2_3_RETRIEVAL.md

# Phase 4: triage. Dry-run first: it prices the uncached calls.
python scripts/eval_triage.py --dry-run
python scripts/eval_triage.py --sweep           # needs ANTHROPIC_API_KEY; reruns are $0 (prompt-hash cache)
python scripts/report_phase4.py --out docs/PHASE4_TRIAGE.md --plot docs/phase4_frontier.png
```

Why day-windowed sampling instead of "the N most recent": GitHub's `event=` listing came back a month stale for pytorch while a `created=` filter returned that day's runs, and re-run evidence needs elapsed time to exist.

Ingest is resumable: runs without jobs get jobs on the next invocation; failed jobs without logs get logs. Labeling recomputes from scratch each time (idempotent by design — a new rule means rerun, not re-ingest).

## Without a token

Everything runs unauthenticated at 60 req/hr, but that budget is roughly one pytorch run's worth of job pages. Log downloads are refused entirely (`403 Must have admin rights`). The smoke test committed here — 8 push runs, 61 jobs, 530 steps — is the extent of what's honest to claim without one.

## Running tests

```bash
docker compose up -d
python -m pytest
```

Tests use a dedicated `winnow_test` database on the same Postgres, created and migrated on first run. The dev database is never touched.

## Layout

```
src/winnow/
  github_client.py   rate-limit-aware client: per-bucket X-RateLimit tracking, Retry-After, Link pagination
  ingest.py          discover runs -> fetch jobs/steps -> fetch failed-job logs (each pass resumable)
  normalize.py       log -> failure signature (pytest summary > traceback > tail fallback)
  label.py           the re-run join: flake / real / infra, plus autoretry-workflow detection
  exclusions.py      registry of every drop rule; nothing is excluded except through here
  external_signals.py  pytorch DISABLED-test bot fetch + per-test agreement
  retry_scan.py      in-process retry markers in workflow YAML (survivorship-bias check)
  signatures.py      runs the normalizer over fetched logs
  retrieval.py       fulltext (tsvector/ts_rank) and trigram retrievers, repo+recency filters in SQL
  retrieval_eval.py  recall@k / MRR against failure_key recurrences, stratified by exact-dup
  vector.py          Embedder protocol, per-distinct-text cache, pgvector HNSW retriever
  triage.py          structured TriageVerdict, prompt rendering, prompt-hash llm_cache
  triage_eval.py     held-out-by-repo operating point, citation resolution, cost/latency
migrations/          forward-only SQL, tracked in schema_migrations
scripts/             CLIs for each phase
docs/adr/            decision records
```
