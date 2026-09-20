# Winnow

A CI failure triage service. Reads public GitHub Actions history and answers one question per failure: is this a flaky test or a real regression, and here is the prior occurrence that proves it.

**Status: Phases 0–1 built.** Local harness, ingest, signature normalizer, and the derived-label join are implemented and tested. Numbers below are from the current corpus and will change as ingest grows. See `docs/PHASE1_RESULTS.md` for the live exit-criterion tables.

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

Volume verified 2026-09-19 via the public API: every one returns dozens of runs inside a single hour. **Log retention per repo is not yet verified** — the job-logs endpoint returns 403 without a token regardless of repo visibility, so that check is blocked on `GITHUB_TOKEN`.

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
# Phase 0: runs, jobs, steps; failed-job logs if a token is present
python scripts/ingest_repo.py --repo pytorch/pytorch --target-runs 500 --fetch-logs

# Phase 1: signatures, labels, exclusions, retry-config scan, external signal
python scripts/label_repo.py --repo pytorch/pytorch

# Exit-criterion tables
python scripts/report_phase1.py --out docs/PHASE1_RESULTS.md
```

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
migrations/          forward-only SQL, tracked in schema_migrations
scripts/             CLIs for each phase
docs/adr/            decision records
```
