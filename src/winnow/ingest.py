from __future__ import annotations

import datetime as dt
import logging
from datetime import datetime, timezone

import psycopg

from winnow.github_client import GitHubClient, GitHubAPIError, LogsUnavailable
from winnow.config import Config

logger = logging.getLogger("winnow.ingest")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def discover_runs(
    client: GitHubClient,
    conn: psycopg.Connection,
    owner: str,
    repo: str,
    repo_id: int,
    per_day: int,
    days: int,
    events: tuple[str, ...] = ("push", "pull_request"),
    end_date: dt.date | None = None,
) -> int:
    """Samples up to `per_day` runs per event type for each of the last
    `days` days, via the `created=YYYY-MM-DD` filter.

    Day windows, not "the N most recent", for two reasons measured on the
    first corpus: (1) GitHub's `event=` listing is not reliably ordered
    for very high-volume repos -- pytorch's "newest" push run came back a
    month stale while `created>=` returned that day's runs; (2) re-run
    evidence needs elapsed time to exist, and 500 runs of a repo doing
    9,000 push runs a day is a few hours, not a history.

    Idempotent: re-running refreshes rows via ON CONFLICT.
    """
    end_date = end_date or dt.datetime.now(dt.timezone.utc).date()
    inserted = 0
    for offset in range(days):
        day = end_date - dt.timedelta(days=offset)
        for event in events:
            seen = 0
            for run in client.list_workflow_runs(owner, repo, event=event, created=day.isoformat()):
                _upsert_run(conn, repo_id, run)
                inserted += 1
                seen += 1
                if seen >= per_day:
                    break
            conn.commit()
        if offset % 5 == 4:
            logger.info("repo=%s/%s discovered through %s: %d runs so far", owner, repo, day, inserted)
    logger.info("repo=%s/%s discovered %d runs across %d days", owner, repo, inserted, days)
    return inserted


def _upsert_run(conn: psycopg.Connection, repo_id: int, run: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflow_runs (
                run_id, repo_id, workflow_id, workflow_name, run_number, event,
                head_branch, head_sha, status, conclusion, latest_run_attempt,
                actor_login, triggering_actor_login, created_at, updated_at,
                run_started_at, html_url
            ) VALUES (
                %(run_id)s, %(repo_id)s, %(workflow_id)s, %(workflow_name)s, %(run_number)s, %(event)s,
                %(head_branch)s, %(head_sha)s, %(status)s, %(conclusion)s, %(run_attempt)s,
                %(actor_login)s, %(triggering_actor_login)s, %(created_at)s, %(updated_at)s,
                %(run_started_at)s, %(html_url)s
            )
            ON CONFLICT (run_id) DO UPDATE SET
                status = EXCLUDED.status,
                conclusion = EXCLUDED.conclusion,
                latest_run_attempt = GREATEST(workflow_runs.latest_run_attempt, EXCLUDED.latest_run_attempt),
                updated_at = EXCLUDED.updated_at
            """,
            {
                "run_id": run["id"],
                "repo_id": repo_id,
                "workflow_id": run["workflow_id"],
                "workflow_name": run.get("name"),
                "run_number": run.get("run_number"),
                "event": run["event"],
                "head_branch": run.get("head_branch"),
                "head_sha": run["head_sha"],
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "run_attempt": run.get("run_attempt", 1),
                "actor_login": (run.get("actor") or {}).get("login"),
                "triggering_actor_login": (run.get("triggering_actor") or {}).get("login"),
                "created_at": _parse_ts(run["created_at"]),
                "updated_at": _parse_ts(run.get("updated_at")),
                "run_started_at": _parse_ts(run.get("run_started_at")),
                "html_url": run.get("html_url"),
            },
        )


def fetch_jobs_for_ingested_runs(
    client: GitHubClient,
    conn: psycopg.Connection,
    owner: str,
    repo: str,
    repo_id: int,
) -> int:
    """For every run already in the DB without jobs fetched yet, pulls
    jobs (all attempts) and steps. Separate pass from discover_runs so a
    partial ingest (rate-limited mid-run) resumes cleanly: runs already
    have their jobs, or don't, and we can tell which from the DB alone.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT wr.run_id FROM workflow_runs wr
            WHERE wr.repo_id = %s
              AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.run_id = wr.run_id)
            ORDER BY wr.created_at DESC
            """,
            (repo_id,),
        )
        run_ids = [row[0] for row in cur.fetchall()]

    total_jobs = 0
    for i, run_id in enumerate(run_ids, 1):
        n = _fetch_jobs_for_run(client, conn, owner, repo, repo_id, run_id)
        total_jobs += n
        conn.commit()
        if i % 25 == 0:
            logger.info("jobs fetched for %d/%d runs (%d jobs so far)", i, len(run_ids), total_jobs)
    return total_jobs


def _fetch_jobs_for_run(
    client: GitHubClient,
    conn: psycopg.Connection,
    owner: str,
    repo: str,
    repo_id: int,
    run_id: int,
) -> int:
    n = 0
    try:
        for job in client.list_jobs_for_run(owner, repo, run_id, all_attempts=True):
            _upsert_job(conn, repo_id, job)
            n += 1
    except GitHubAPIError as exc:
        logger.error("failed to fetch jobs for run_id=%s: %s", run_id, exc)
    return n


def _upsert_job(conn: psycopg.Connection, repo_id: int, job: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (
                job_id, run_id, repo_id, name, run_attempt, status, conclusion,
                started_at, completed_at, runner_name, runner_group_name, labels, html_url
            ) VALUES (
                %(job_id)s, %(run_id)s, %(repo_id)s, %(name)s, %(run_attempt)s, %(status)s, %(conclusion)s,
                %(started_at)s, %(completed_at)s, %(runner_name)s, %(runner_group_name)s, %(labels)s, %(html_url)s
            )
            ON CONFLICT (job_id) DO UPDATE SET
                status = EXCLUDED.status,
                conclusion = EXCLUDED.conclusion,
                completed_at = EXCLUDED.completed_at
            """,
            {
                "job_id": job["id"],
                "run_id": job["run_id"],
                "repo_id": repo_id,
                "name": job["name"],
                "run_attempt": job.get("run_attempt", 1),
                "status": job.get("status"),
                "conclusion": job.get("conclusion"),
                "started_at": _parse_ts(job.get("started_at")),
                "completed_at": _parse_ts(job.get("completed_at")),
                "runner_name": job.get("runner_name"),
                "runner_group_name": job.get("runner_group_name"),
                "labels": psycopg.types.json.Json(job.get("labels") or []),
                "html_url": job.get("html_url"),
            },
        )
        for step in job.get("steps") or []:
            cur.execute(
                """
                INSERT INTO steps (job_id, name, number, status, conclusion, started_at, completed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    job["id"],
                    step.get("name"),
                    step.get("number"),
                    step.get("status"),
                    step.get("conclusion"),
                    _parse_ts(step.get("started_at")),
                    _parse_ts(step.get("completed_at")),
                ),
            )


def fetch_failed_job_logs(
    client: GitHubClient,
    conn: psycopg.Connection,
    owner: str,
    repo: str,
    cfg: Config,
    limit: int | None = None,
) -> dict[str, int]:
    """Downloads logs for failed jobs only (per spec: successful-job logs
    are the overwhelming majority of volume and worthless for retrieval).
    Requires GITHUB_TOKEN. Writes gzip-free raw text locally under
    cfg.log_dir/{job_id}.log (Phase 5 moves this to S3); records outcome
    in jobs.log_fetch_status so retention limits are visible per repo
    rather than silently dropped.
    """
    counts = {"fetched": 0, "expired_410": 0, "forbidden_403": 0, "error": 0, "skipped_no_token": 0}
    if not client.token:
        logger.warning("GITHUB_TOKEN not set -- cannot fetch job logs, skipping entirely")
        counts["skipped_no_token"] = 1
        return counts

    cfg.log_dir.mkdir(parents=True, exist_ok=True)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id FROM jobs
            WHERE repo_id = (SELECT id FROM repos WHERE owner = %s AND name = %s)
              AND conclusion = 'failure'
              AND log_fetch_status = 'not_attempted'
            ORDER BY job_id DESC
            """
            + (" LIMIT %s" if limit else ""),
            (owner, repo, limit) if limit else (owner, repo),
        )
        job_ids = [row[0] for row in cur.fetchall()]

    for i, job_id in enumerate(job_ids, 1):
        status, path = _fetch_one_log(client, conn, owner, repo, job_id, cfg)
        counts[status] = counts.get(status, 0) + 1
        _mark_log_status(conn, job_id, status, path)
        if i % 50 == 0:
            conn.commit()
            logger.info("logs fetched for %d/%d failed jobs", i, len(job_ids))
    conn.commit()
    return counts


def _fetch_one_log(
    client: GitHubClient, conn: psycopg.Connection, owner: str, repo: str, job_id: int, cfg: Config
) -> tuple[str, str | None]:
    try:
        text = client.get_job_logs_text(owner, repo, job_id)
    except LogsUnavailable as exc:
        if exc.status_code == 410:
            return "expired_410", None
        if exc.status_code == 403:
            return "forbidden_403", None
        return "error", None
    except GitHubAPIError:
        return "error", None

    path = cfg.log_dir / f"{job_id}.log"
    path.write_text(text, encoding="utf-8", errors="replace")
    return "fetched", str(path)


def _mark_log_status(conn: psycopg.Connection, job_id: int, status: str, path: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET log_fetch_status = %s, log_local_path = %s WHERE job_id = %s",
            (status, path, job_id),
        )
