-- Winnow Phase 0/1 schema.
-- Design notes:
--   * run_id and job_id are GitHub's own bigint ids -- globally unique, used
--     directly as primary keys rather than inventing a surrogate.
--   * "same job across attempts" is (run_id, name): GitHub keeps run_id and
--     head_sha fixed across a re-run, and only run_attempt / job_id change.
--   * labels and exclusions are separate from jobs/runs so re-labeling
--     (a new labeling rule, a bug fix in the join) never touches raw ingest
--     data -- rerun the labeler, don't re-ingest.

CREATE TABLE IF NOT EXISTS repos (
    id                  SERIAL PRIMARY KEY,
    owner               TEXT NOT NULL,
    name                TEXT NOT NULL,
    default_branch      TEXT NOT NULL,
    has_external_flaky_signal BOOLEAN NOT NULL DEFAULT FALSE,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner, name)
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id              BIGINT PRIMARY KEY,
    repo_id             INTEGER NOT NULL REFERENCES repos(id),
    workflow_id         BIGINT NOT NULL,
    workflow_name       TEXT,
    run_number          INTEGER,
    event               TEXT NOT NULL,          -- push, pull_request, schedule, ...
    head_branch         TEXT,
    head_sha            TEXT NOT NULL,
    status              TEXT,
    conclusion          TEXT,
    latest_run_attempt  INTEGER NOT NULL,        -- highest run_attempt observed
    actor_login         TEXT,
    triggering_actor_login TEXT,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ,
    run_started_at      TIMESTAMPTZ,
    html_url            TEXT,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_runs_repo_created ON workflow_runs (repo_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_repo_sha ON workflow_runs (repo_id, head_sha);
CREATE INDEX IF NOT EXISTS idx_runs_event_branch ON workflow_runs (repo_id, event, head_branch);

CREATE TABLE IF NOT EXISTS jobs (
    job_id              BIGINT PRIMARY KEY,
    run_id              BIGINT NOT NULL REFERENCES workflow_runs(run_id),
    repo_id             INTEGER NOT NULL REFERENCES repos(id),
    name                TEXT NOT NULL,
    run_attempt         INTEGER NOT NULL,
    status              TEXT,
    conclusion          TEXT,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    runner_name         TEXT,
    runner_group_name   TEXT,
    labels              JSONB,                  -- matrix / runner labels, e.g. ["ubuntu-latest"]
    html_url            TEXT,
    log_fetch_status    TEXT NOT NULL DEFAULT 'not_attempted',
                        -- not_attempted | fetched | expired_410 | forbidden_403 | error
    log_local_path      TEXT,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs (run_id);
CREATE INDEX IF NOT EXISTS idx_jobs_repo_name ON jobs (repo_id, name);
CREATE INDEX IF NOT EXISTS idx_jobs_conclusion ON jobs (repo_id, conclusion);

CREATE TABLE IF NOT EXISTS steps (
    id                  BIGSERIAL PRIMARY KEY,
    job_id              BIGINT NOT NULL REFERENCES jobs(job_id),
    name                TEXT,
    number              INTEGER,
    status              TEXT,
    conclusion          TEXT,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_steps_job ON steps (job_id);

-- Failure signature: one row per job that failed and whose log we parsed.
-- Phase 1 (normalize.py) populates this. Raw log bytes live under
-- WINNOW_LOG_DIR locally (S3 in Phase 5); this table holds the derived,
-- deterministic-preprocessing signature only.
CREATE TABLE IF NOT EXISTS failure_signatures (
    job_id              BIGINT PRIMARY KEY REFERENCES jobs(job_id),
    signature_source    TEXT NOT NULL,   -- pytest_failed_summary | traceback_block | tail_fallback
    exception_type      TEXT,
    message_skeleton    TEXT,
    top_frames          JSONB,           -- list of {"file":..., "function":...}
    test_nodeids        JSONB,           -- list of pytest node ids found failing in this job's log
    signature_text      TEXT NOT NULL,   -- human-readable normalized text, indexed in Phase 2
    signature_hash      TEXT NOT NULL,   -- sha256 of normalized signature, used for grouping + embed cache key
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sig_hash ON failure_signatures (signature_hash);

-- One row per (job_id) that was a candidate for labeling, i.e. a failed
-- job on a push-to-default-branch run. label is the derived ground truth;
-- label_reason is the specific rule that produced it, so the join is
-- auditable end to end.
CREATE TABLE IF NOT EXISTS labels (
    job_id              BIGINT PRIMARY KEY REFERENCES jobs(job_id),
    run_id              BIGINT NOT NULL REFERENCES workflow_runs(run_id),
    repo_id             INTEGER NOT NULL REFERENCES repos(id),
    label               TEXT NOT NULL CHECK (label IN ('flake', 'real', 'infra')),
    label_reason        TEXT NOT NULL,
    resolved_run_id     BIGINT,          -- for 'real': the later push run whose same job succeeded
    resolved_job_id     BIGINT,          -- for 'flake': the later-attempt job_id that succeeded
    resolved_sha        TEXT,
    attempts_to_resolution INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_labels_repo_label ON labels (repo_id, label);

-- Every record dropped before labeling, with the specific rule and reason.
-- This table IS the Phase 1 exit-criterion table -- report.py renders it.
CREATE TABLE IF NOT EXISTS exclusions (
    id                  BIGSERIAL PRIMARY KEY,
    repo_id             INTEGER REFERENCES repos(id),
    scope               TEXT NOT NULL,   -- run | job | repo | workflow
    scope_id            TEXT NOT NULL,
    rule                TEXT NOT NULL,   -- see winnow.exclusions.Rule
    detail              TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_exclusions_rule ON exclusions (rule);

-- External flakiness signal (e.g. pytorch's DISABLED-test bot issues),
-- used in Phase 1 to measure agreement against derived labels.
CREATE TABLE IF NOT EXISTS external_flaky_signals (
    id                  BIGSERIAL PRIMARY KEY,
    repo_id             INTEGER NOT NULL REFERENCES repos(id),
    test_class          TEXT,
    test_name           TEXT NOT NULL,
    source              TEXT NOT NULL,   -- e.g. 'pytorch_disabled_test_issue'
    source_url          TEXT,
    state                TEXT,           -- open | closed (closed = re-enabled)
    reported_at         TIMESTAMPTZ,
    raw                 JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ext_flaky_repo_test ON external_flaky_signals (repo_id, test_name);

-- Repo-level notes on in-process retry config detected by grepping
-- workflow YAML (contaminating case 3: pytest-rerunfailures etc.).
-- Best-effort; see docs on why this can't be a per-job exclusion.
CREATE TABLE IF NOT EXISTS retry_config_findings (
    id                  BIGSERIAL PRIMARY KEY,
    repo_id             INTEGER NOT NULL REFERENCES repos(id),
    workflow_path       TEXT NOT NULL,
    marker              TEXT NOT NULL,   -- the string that matched, e.g. 'reruns'
    context_line        TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
