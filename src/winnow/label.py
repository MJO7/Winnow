"""The re-run-outcome labeling join (Phase 1 core).

Ground truth: a completed job on a push-to-default-branch run is FLAKE if
the same (run_id, job name) group's *last* attempt succeeded; REAL if the
last attempt never succeeded and a later push, on a commit confirmed to
be a descendant of the failing one, later runs that same job to success;
INFRA if a setup/teardown-lifecycle step -- not a test-running step --
is what failed. Everything else that would corrupt that join is recorded
in `exclusions`, never silently dropped and never silently mislabeled.

One label per (run_id, job name) group, keyed to the group's *final*
attempt. Earlier failing attempts within the same group aren't separately
labeled -- they're context for the group's outcome (see
`attempts_to_resolution`), not independent events. This is what makes
"fail, fail, pass on the third attempt" (spec case 6) fall out of the
same rule as the two-attempt case instead of needing special handling.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

import psycopg

from winnow.exclusions import Rule, record as record_exclusion, tally as tally_exclusions
from winnow.github_client import GitHubClient, GitHubAPIError
from winnow.repos import RepoSpec

logger = logging.getLogger("winnow.label")

# Step names GitHub Actions injects around user-defined steps. A failure
# here is infrastructure by definition -- no user test code has run yet,
# or the job died tearing down rather than testing anything.
INFRA_STEP_MARKERS = (
    "Set up job",
    "Set up runner",
    "Initialize containers",
    "Complete job",
    "Checking out the ref",
    "Wait for other jobs to be defined",
    "Post Checkout",
)

# Auto-retry-workflow heuristic (contaminating case 4): a bot/action that
# blindly re-runs a failed workflow fires quickly and near-uniformly. A
# human clicking "re-run failed jobs" does not -- delay ranges from
# minutes to days and has high variance. This is a heuristic, not a
# certainty; see docs/adr for the false-positive/negative discussion.
AUTORETRY_MAX_MEAN_DELAY_SECONDS = 5 * 60
AUTORETRY_CV_THRESHOLD = 0.15
AUTORETRY_MIN_SAMPLES = 5

MAX_FORWARD_CANDIDATES = 8


@dataclass
class LabelSummary:
    flake: int = 0
    real: int = 0
    infra: int = 0
    excluded_by_rule: dict[str, int] | None = None


def detect_autoretry_workflows(conn: psycopg.Connection, repo_id: int) -> set[int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT wr.workflow_id, EXTRACT(EPOCH FROM (j2.started_at - j1.completed_at)) AS delay_s
            FROM jobs j1
            JOIN jobs j2
              ON j1.run_id = j2.run_id AND j1.name = j2.name AND j2.run_attempt = j1.run_attempt + 1
            JOIN workflow_runs wr ON wr.run_id = j1.run_id
            WHERE j1.repo_id = %s
              AND j1.completed_at IS NOT NULL AND j2.started_at IS NOT NULL
            """,
            (repo_id,),
        )
        rows = cur.fetchall()

    by_workflow: dict[int, list[float]] = {}
    for workflow_id, delay_s in rows:
        if delay_s is None or delay_s < 0:
            continue
        by_workflow.setdefault(workflow_id, []).append(float(delay_s))

    flagged = set()
    for workflow_id, delays in by_workflow.items():
        if len(delays) < AUTORETRY_MIN_SAMPLES:
            continue
        mean = statistics.mean(delays)
        if mean <= 0 or mean > AUTORETRY_MAX_MEAN_DELAY_SECONDS:
            continue
        cv = statistics.pstdev(delays) / mean
        if cv <= AUTORETRY_CV_THRESHOLD:
            flagged.add(workflow_id)
            logger.info(
                "workflow_id=%s flagged auto-retry: n=%d mean_delay=%.1fs cv=%.3f",
                workflow_id, len(delays), mean, cv,
            )
    return flagged


def _mark_non_qualifying_runs(conn: psycopg.Connection, repo_id: int, default_branch: str) -> list[int]:
    """Records the PR/non-default-branch exclusion (spec case 2) and
    returns the run_ids that DO qualify for the labeled corpus. PR runs
    are still ingested -- they feed the retrieval corpus in Phase 2/3 --
    they just never enter `labels`.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, event, head_branch, status, conclusion
            FROM workflow_runs
            WHERE repo_id = %s
            """,
            (repo_id,),
        )
        rows = cur.fetchall()

    qualifying: list[int] = []
    for run_id, event, head_branch, status, conclusion in rows:
        if event != "push" or head_branch != default_branch:
            record_exclusion(
                conn, repo_id, "run", run_id, Rule.NON_PUSH_OR_NON_DEFAULT_BRANCH,
                detail=f"event={event} head_branch={head_branch}",
            )
            continue
        if status != "completed":
            record_exclusion(conn, repo_id, "run", run_id, Rule.RUN_NOT_COMPLETED, detail=f"status={status}")
            continue
        if conclusion == "cancelled":
            record_exclusion(conn, repo_id, "run", run_id, Rule.RUN_CANCELLED_SUPERSEDED)
            continue
        qualifying.append(run_id)
    return qualifying


def _is_infra_by_steps(conn: psycopg.Connection, job_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT name, conclusion FROM steps WHERE job_id = %s", (job_id,))
        rows = cur.fetchall()
    for name, conclusion in rows:
        if not name or conclusion not in ("failure", "cancelled", "timed_out"):
            continue
        if any(marker.lower() in name.lower() for marker in INFRA_STEP_MARKERS):
            return True
    return False


@dataclass
class _Resolution:
    run_id: int
    sha: str


def _resolve_forward(
    conn: psycopg.Connection,
    client: GitHubClient,
    spec: RepoSpec,
    repo_id: int,
    workflow_id: int,
    job_name: str,
    failing_run_id: int,
) -> tuple[_Resolution | None, bool]:
    """Looks for the first later push-to-default-branch run where this
    same job succeeded, then confirms via the compare API that the later
    commit is actually a descendant of the failing one (spec case 1:
    force-push / history rewrite must not count as "the same code got
    fixed"). Returns (resolution_or_None, saw_diverged_candidate).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT head_sha, created_at FROM workflow_runs WHERE run_id = %s", (failing_run_id,))
        failing_sha, _ = cur.fetchone()

        cur.execute(
            """
            WITH latest_attempt AS (
                SELECT DISTINCT ON (wr.run_id)
                    wr.run_id, wr.head_sha, wr.created_at, j.conclusion
                FROM workflow_runs wr
                JOIN jobs j ON j.run_id = wr.run_id AND j.name = %s
                WHERE wr.repo_id = %s AND wr.workflow_id = %s
                  AND wr.event = 'push' AND wr.head_branch = %s
                  AND wr.status = 'completed'
                  AND wr.created_at > (SELECT created_at FROM workflow_runs WHERE run_id = %s)
                ORDER BY wr.run_id, j.run_attempt DESC
            )
            SELECT run_id, head_sha, conclusion FROM latest_attempt ORDER BY created_at ASC
            """,
            (job_name, repo_id, workflow_id, spec.default_branch, failing_run_id),
        )
        candidates = cur.fetchall()

    saw_diverged = False
    checked = 0
    for run_id2, sha2, conclusion2 in candidates:
        if conclusion2 != "success":
            continue
        if checked >= MAX_FORWARD_CANDIDATES:
            break
        checked += 1
        try:
            cmp = client.compare_commits(spec.owner, spec.name, failing_sha, sha2)
        except GitHubAPIError as exc:
            logger.warning("compare_commits failed for %s...%s: %s", failing_sha, sha2, exc)
            continue
        if cmp.get("status") == "ahead":
            return _Resolution(run_id=run_id2, sha=sha2), saw_diverged
        if cmp.get("status") in ("diverged", "behind"):
            saw_diverged = True
            continue
    return None, saw_diverged


def _clear_prior_results(conn: psycopg.Connection, repo_id: int) -> None:
    """label_repo recomputes from scratch every time it's run, rather than
    incrementally patching -- with no unique key on `exclusions`, a rerun
    would otherwise duplicate every row and silently double every count
    in the exit report. Rerunning the labeler is meant to be free and
    idempotent (new labeling rule, bugfix in the join); it should never
    require re-ingesting first.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM exclusions WHERE repo_id = %s", (repo_id,))
        cur.execute("DELETE FROM labels WHERE repo_id = %s", (repo_id,))
    conn.commit()


def label_repo(
    client: GitHubClient, conn: psycopg.Connection, repo_id: int, spec: RepoSpec
) -> LabelSummary:
    _clear_prior_results(conn, repo_id)
    autoretry_workflows = detect_autoretry_workflows(conn, repo_id)
    qualifying_run_ids = _mark_non_qualifying_runs(conn, repo_id, spec.default_branch)
    conn.commit()

    if not qualifying_run_ids:
        return LabelSummary(excluded_by_rule={})

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.run_id, j.name, wr.workflow_id, wr.head_sha,
                   array_agg(j.job_id ORDER BY j.run_attempt) AS job_ids,
                   array_agg(j.run_attempt ORDER BY j.run_attempt) AS attempts,
                   array_agg(j.conclusion ORDER BY j.run_attempt) AS conclusions
            FROM jobs j
            JOIN workflow_runs wr ON wr.run_id = j.run_id
            WHERE j.run_id = ANY(%s) AND j.conclusion IS NOT NULL AND j.conclusion != 'skipped'
            GROUP BY j.run_id, j.name, wr.workflow_id, wr.head_sha
            """,
            (qualifying_run_ids,),
        )
        groups = cur.fetchall()

    summary = LabelSummary()

    for run_id, name, workflow_id, head_sha, job_ids, attempts, conclusions in groups:
        if workflow_id in autoretry_workflows:
            for jid in job_ids:
                record_exclusion(conn, repo_id, "job", jid, Rule.WORKFLOW_AUTORETRY_SUSPECTED, detail=f"workflow_id={workflow_id}")
            continue

        last_conclusion = conclusions[-1]
        last_job_id = job_ids[-1]

        if last_conclusion == "success":
            failing_job_ids = [jid for jid, c in zip(job_ids, conclusions) if c == "failure"]
            for jid in failing_job_ids:
                _insert_label(
                    conn, jid, run_id, repo_id, "flake", "rerun_same_run_id_succeeded",
                    resolved_job_id=last_job_id, resolved_sha=head_sha,
                    attempts_to_resolution=attempts[-1],
                )
                summary.flake += 1
            continue

        if last_conclusion not in ("failure", "timed_out"):
            # action_required / neutral / stale: not a completed test
            # outcome we can reason about with this join. Rare enough at
            # this corpus's scale to not warrant its own rule; visible in
            # logs if it ever dominates.
            continue

        if _is_infra_by_steps(conn, last_job_id):
            _insert_label(conn, last_job_id, run_id, repo_id, "infra", "infra_setup_step_failed")
            summary.infra += 1
            continue

        resolution, saw_diverged = _resolve_forward(conn, client, spec, repo_id, workflow_id, name, run_id)
        if resolution is not None:
            _insert_label(
                conn, last_job_id, run_id, repo_id, "real", "no_success_then_fixed_by_later_push",
                resolved_run_id=resolution.run_id, resolved_sha=resolution.sha,
            )
            summary.real += 1
        elif saw_diverged:
            record_exclusion(conn, repo_id, "job", last_job_id, Rule.FORCE_PUSH_OR_DIVERGED_HISTORY)
        else:
            record_exclusion(conn, repo_id, "job", last_job_id, Rule.NO_RESOLUTION_WITHIN_INGEST_WINDOW)

    conn.commit()

    summary.excluded_by_rule = dict(tally_exclusions(conn, repo_id))
    return summary


def _insert_label(
    conn: psycopg.Connection,
    job_id: int,
    run_id: int,
    repo_id: int,
    label: str,
    reason: str,
    resolved_run_id: int | None = None,
    resolved_job_id: int | None = None,
    resolved_sha: str | None = None,
    attempts_to_resolution: int | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO labels (
                job_id, run_id, repo_id, label, label_reason,
                resolved_run_id, resolved_job_id, resolved_sha, attempts_to_resolution
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (job_id) DO UPDATE SET
                label = EXCLUDED.label,
                label_reason = EXCLUDED.label_reason,
                resolved_run_id = EXCLUDED.resolved_run_id,
                resolved_job_id = EXCLUDED.resolved_job_id,
                resolved_sha = EXCLUDED.resolved_sha,
                attempts_to_resolution = EXCLUDED.attempts_to_resolution
            """,
            (job_id, run_id, repo_id, label, reason, resolved_run_id, resolved_job_id, resolved_sha, attempts_to_resolution),
        )
