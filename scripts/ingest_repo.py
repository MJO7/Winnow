#!/usr/bin/env python3
"""Phase 0 ingest CLI.

Usage:
    python scripts/ingest_repo.py --repo pytorch/pytorch --days 30 --per-day 15
    python scripts/ingest_repo.py --repo pytorch/pytorch --days 30 --per-day 15 --fetch-logs

Steps, each idempotent and resumable:
  1. discover runs (up to --per-day per event type for each of the last --days days)
  2. fetch jobs + steps for any run that doesn't have them yet
  3. optionally fetch logs for failed jobs only (requires GITHUB_TOKEN)
"""
import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from winnow.config import load_config
from winnow.db import get_conn, get_or_create_repo
from winnow.github_client import GitHubClient
from winnow.ingest import discover_runs, fetch_jobs_for_ingested_runs, fetch_failed_job_logs
from winnow.repos import get_repo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingest_repo")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name, must be in winnow.repos.CORPUS")
    ap.add_argument("--days", type=int, default=30, help="how many days back to sample")
    ap.add_argument("--per-day", type=int, default=15, help="runs to discover per event type per day")
    ap.add_argument("--events", default="push,pull_request")
    ap.add_argument("--fetch-logs", action="store_true", help="also download logs for failed jobs")
    ap.add_argument("--log-limit", type=int, default=None, help="cap on failed-job logs fetched this run")
    args = ap.parse_args()

    spec = get_repo(args.repo)
    cfg = load_config()
    client = GitHubClient(token=cfg.github_token)

    if not cfg.github_token:
        logger.warning(
            "GITHUB_TOKEN not set: rate limit is 60 req/hr (vs 5000 authenticated), "
            "and job log downloads will be skipped entirely. Metadata-only ingest "
            "at this scale will be slow and may not finish before "
            "the hour resets."
        )

    t0 = time.time()
    with get_conn() as conn:
        repo_id = get_or_create_repo(
            conn, spec.owner, spec.name, spec.default_branch, spec.has_external_flaky_signal
        )

        events = tuple(e.strip() for e in args.events.split(",") if e.strip())
        n_runs = discover_runs(client, conn, spec.owner, spec.name, repo_id, args.per_day, args.days, events)
        logger.info("discovered/updated %d run records", n_runs)

        n_jobs = fetch_jobs_for_ingested_runs(client, conn, spec.owner, spec.name, repo_id)
        logger.info("fetched %d job records", n_jobs)

        if args.fetch_logs:
            counts = fetch_failed_job_logs(client, conn, spec.owner, spec.name, cfg, limit=args.log_limit)
            logger.info("log fetch results: %s", counts)

    elapsed = time.time() - t0
    rl = client.rate_limit_snapshot()
    logger.info("done in %.1fs. rate limit snapshot: %s", elapsed, {k: (v.remaining, v.limit) for k, v in rl.items()})


if __name__ == "__main__":
    main()
