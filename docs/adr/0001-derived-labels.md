# ADR 0001: Derived labels from re-run outcome, not hand-labeling

**Status:** accepted, validation pending token-backed log corpus
**Date:** 2026-09-19

## Decision

A failed CI job's label is derived from what happened to that job afterwards, never assigned by a human:

| Label | Rule | Evidence stored |
|---|---|---|
| flake | Same `(run_id, job name)` group: a later `run_attempt` concluded `success`. GitHub pins `run_id` and `head_sha` across attempts, so "same code, different outcome" is guaranteed by the platform, not inferred. | `resolved_job_id`, `attempts_to_resolution` |
| real | Group's last attempt never succeeded; a later push-to-default-branch run of the same workflow + job name concluded `success`, **and** `GET /compare/{failing_sha}...{fixed_sha}` reports `status: ahead`. | `resolved_run_id`, `resolved_sha` |
| infra | A GitHub-injected lifecycle step (`Set up job`, `Initialize containers`, `Complete job`, …) has conclusion failure/cancelled. Checked before forward resolution — an infra failure is not a candidate for "fixed by a later commit". | `label_reason = infra_setup_step_failed` |

One label per `(run_id, job name)` group, keyed to the final attempt. Earlier failing attempts are context, not independent events. This is why fail-fail-pass on a third attempt needs no special case: the group's last attempt succeeded, every failing attempt in it is a flake.

## Why re-run outcome is a valid proxy

The alternative is hand-labeling, which caps the eval set at a few hundred examples and makes the labeler's biases the ceiling on everything downstream. Re-run outcome is what an engineer actually does to decide a failure was flaky; the label is the engineer's own action, recorded by the platform.

## Where the proxy is known to fail, and what was done

Each is an exclusion rule in `winnow.exclusions.Rule`; each rule's dropped-record count appears in `docs/PHASE1_RESULTS.md`.

1. **PR runs.** `pull_request` events check out a merge of head with a *moving* base. The metadata `head_sha` is stable while the tested content isn't. Labeled corpus restricted to `push` on the default branch (`non_push_or_non_default_branch`). PR runs still feed retrieval.
2. **Force push / history rewrite.** A later green run on the same branch isn't evidence the failing commit was fixed if history was rewritten. The compare API's `diverged`/`behind` status excludes it (`force_push_or_diverged_history`).
3. **In-process retries.** See below. Cannot be a per-job exclusion; stated as a directional bias.
4. **Workflow-level auto-retry.** A bot re-running a whole workflow inflates the flake class. Detected per workflow by attempt-to-attempt delay: ≥5 samples, mean ≤ 5 min, coefficient of variation ≤ 0.15 → all jobs in that workflow excluded (`workflow_autoretry_suspected`). Thresholds are a heuristic; the unit tests pin both the flagged and the not-flagged case.
5. **Superseded runs.** Whole run `cancelled` (typically a concurrency group cancelling on a newer push) is not a test outcome (`run_cancelled_superseded`).
6. **Window edge.** A last-attempt failure with no later green run inside the ingested window is indistinguishable from "real, not fixed yet". Excluded, not guessed (`no_resolution_within_ingest_window`).

## The in-process retry bias, measured as far as it can be

pytorch's workflow YAML contains 13 retry markers, all in build / upload / tagging steps — none in the test-invocation path. The YAML scan would have called pytorch clean. It is not: `test/run_test.py` sets `PYTORCH_NUM_PYTEST_RERUNS=2` and invokes `pytest --reruns=2`, so a pytorch test that fails once and passes on either in-process retry never reaches the job's conclusion. Verified against `main` on 2026-09-19.

**Direction:** the derived flake population for pytorch is the *hard* flakes — those that failed three times in-process and then passed on a job-level re-run. Flake counts are an undercount; the flake class is skewed toward the most persistent failures, which makes the downstream classification problem harder than a "true" flake distribution would.

**Magnitude:** not derivable from the failed-job log corpus, because the evidence (`=== RERUNS ===` sections) lives in *successful* job logs, which are deliberately not fetched. A bound would require sampling successful-job logs for pytorch specifically. Left as a stated limitation.

## Validation against an independent signal

pytorch's flaky-test bot files `DISABLED test_name (module.Class)` issues under `module: flaky-tests` — 6,100+ open as of 2026-09-19. Agreement is computed per *test*: a derived-flaky test is one appearing in the failing-test set (`failure_signatures.test_nodeids`) of at least one job labeled flake. `winnow.external_signals.compute_agreement` reports precision and recall of the derived-flake label against the bot's set, plus the three disagreement buckets.

**This measurement is blocked on `GITHUB_TOKEN`:** per-test attribution needs parsed logs, and the job-logs endpoint refuses unauthenticated requests on public repos. The code path is built and reports "no per-test labels yet" rather than a vacuous 100%.
