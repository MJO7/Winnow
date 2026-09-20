#!/usr/bin/env python3
"""Phase 1 CLI: signatures, labels, exclusions, retry-config scan, and
(for repos that have one) the external flakiness signal.

    python scripts/label_repo.py --repo pytorch/pytorch
    python scripts/label_repo.py --repo pytorch/pytorch --skip-external

Everything here is a pure function of what's already in Postgres plus
the compare-commits calls the forward-resolution join needs. Re-running
is idempotent: labels and exclusions are recomputed from scratch.
"""
import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from winnow.config import load_config
from winnow.db import get_conn, get_or_create_repo
from winnow.external_signals import (
    PYTORCH_SOURCE,
    compute_agreement,
    fetch_pytorch_disabled_tests,
    store_signals,
)
from winnow.github_client import GitHubClient
from winnow.label import label_repo
from winnow.repos import get_repo
from winnow.retry_scan import scan_repo, summarize as summarize_retry
from winnow.signatures import build_signatures

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("label_repo")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--skip-external", action="store_true", help="don't (re)fetch the external flaky signal")
    ap.add_argument("--skip-retry-scan", action="store_true")
    ap.add_argument("--external-since", default="2024-01-01", help="earliest issue creation date to fetch")
    args = ap.parse_args()

    spec = get_repo(args.repo)
    cfg = load_config()
    client = GitHubClient(token=cfg.github_token)

    with get_conn() as conn:
        repo_id = get_or_create_repo(conn, spec.owner, spec.name, spec.default_branch, spec.has_external_flaky_signal)

        sig_counts = build_signatures(conn, repo_id)
        logger.info("signatures by source: %s", sig_counts or "none (no fetched logs)")

        if not args.skip_retry_scan:
            n = scan_repo(client, conn, repo_id, spec)
            logger.info("retry-config findings: %d -> %s", n, summarize_retry(conn, repo_id))

        summary = label_repo(client, conn, repo_id, spec)
        logger.info("labels: flake=%d real=%d infra=%d", summary.flake, summary.real, summary.infra)
        logger.info("exclusions: %s", summary.excluded_by_rule)

        if spec.has_external_flaky_signal and not args.skip_external:
            since = dt.date.fromisoformat(args.external_since)
            tests = fetch_pytorch_disabled_tests(client, since=since)
            n = store_signals(conn, repo_id, tests, PYTORCH_SOURCE)
            logger.info("stored %d external flaky signals", n)

        if spec.has_external_flaky_signal:
            agreement = compute_agreement(conn, repo_id)
            logger.info("agreement: %s", agreement)
            logger.info(
                "derived-flake precision vs bot: %s  recall: %s",
                agreement.precision_of_derived_flake,
                agreement.recall_of_derived_flake,
            )

    rl = client.rate_limit_snapshot()
    logger.info("rate limit snapshot: %s", {k: (v.remaining, v.limit) for k, v in rl.items()})


if __name__ == "__main__":
    main()
