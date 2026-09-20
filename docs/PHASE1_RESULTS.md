# Phase 1 results: derived labels and exclusions

## pytorch/pytorch

| Corpus | Count |
|---|---|
| Workflow runs ingested | 8 |
| Job records | 61 |
| Failed jobs | 0 |
| Failed-job logs fetched | 0 |
| Failed-job logs expired (410) | 0 |
| Failed-job logs not attempted | 0 |

| Label | Count |
|---|---|
| flake | 0 |
| real | 0 |
| infra | 0 |

| Exclusion rule | Records dropped | Reason |
|---|---|---|
| `non_push_or_non_default_branch` | 4 | Run is a pull_request/schedule/etc. event, or a push to a non-default branch. PR head SHAs don't pin the tested content (the checkout is a merge with a moving base), so 'same SHA re-ran and passed' is not a valid flake signal. Still ingested for retrieval. |
| `run_not_completed` | 0 | Run still queued/in progress at ingest time; outcome not yet knowable. |
| `run_cancelled_superseded` | 0 | Whole run concluded 'cancelled' -- typically a concurrency group cancelling in-progress runs on a newer push. Not a test outcome. |
| `no_resolution_within_ingest_window` | 0 | Job's last attempt failed and no later push-to-default-branch run of the same job succeeded inside the ingested window. Can't distinguish 'real, unfixed yet' from 'window too short'. |
| `force_push_or_diverged_history` | 0 | A later run of the job succeeded, but the compare API reports its commit is not a descendant of the failing commit (diverged/behind) -- history was rewritten, so the 'fixed by a later commit' inference doesn't hold. |
| `workflow_autoretry_suspected` | 0 | Workflow's attempt-to-attempt delay is short and near-uniform across >=5 runs (mean <= 5 min, CV <= 0.15): the signature of a bot auto-retrying, which inflates the flake class. All jobs in the workflow excluded. |

**In-process retry markers in workflow YAML** (survivorship-bias check):

| Marker | Occurrences |
|---|---|
| generic retry step | 7 |
| max-attempts | 3 |
| nick-fields/retry action | 3 |

**Agreement with external flaky-test signal** (pytorch DISABLED-test bot):

| Metric | Value |
|---|---|
| Tests the bot has disabled as flaky | 1662 |
| Distinct tests in derived-flake jobs | 0 |
| Distinct tests in derived-real jobs | 0 |
| Overlap: derived flaky AND bot flaky | 0 |
| Derived flaky, bot silent | 0 |
| Bot flaky, we called real | 0 |
| Bot flaky, never observed failing in our window | 1662 |
| Precision of derived flake vs bot | n/a |
| Recall of derived flake vs bot (over observed) | n/a |

_No per-test derived labels yet: agreement requires fetched + parsed failed-job logs (`failure_signatures.test_nodeids`), which requires GITHUB_TOKEN._
