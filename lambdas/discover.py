"""discover: sample runs per day per repo, upsert run metadata, enqueue
one SQS message per run for `fetch`. Emits IngestLagSeconds.

Event (all optional): {"days": 1, "per_day": 15, "repos": ["owner/name"]}
"""
from __future__ import annotations

import datetime as dt
import json
import os

import boto3

from common import configure_db_env, emit_metric, github_token, repos as default_repos
from winnow.db import get_conn, get_or_create_repo
from winnow.github_client import GitHubClient
from winnow.ingest import discover_runs
from winnow.repos import get_repo

_sqs = boto3.client("sqs")


def handler(event, context):
    configure_db_env()
    event = event or {}
    days = int(event.get("days", 1))
    per_day = int(event.get("per_day", 15))
    targets = event.get("repos") or default_repos()
    client = GitHubClient(token=github_token())

    queued = 0
    newest = None
    with get_conn() as conn:
        for full in targets:
            spec = get_repo(full)
            repo_id = get_or_create_repo(conn, spec.owner, spec.name, spec.default_branch, spec.has_external_flaky_signal)
            discover_runs(client, conn, spec.owner, spec.name, repo_id, per_day, days)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT run_id, created_at FROM workflow_runs wr
                    WHERE repo_id = %s AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.run_id = wr.run_id)
                    """,
                    (repo_id,),
                )
                rows = cur.fetchall()
            for run_id, created_at in rows:
                _sqs.send_message(
                    QueueUrl=os.environ["WINNOW_JOB_QUEUE_URL"],
                    MessageBody=json.dumps({"owner": spec.owner, "repo": spec.name, "repo_id": repo_id, "run_id": run_id}),
                )
                queued += 1
                if newest is None or created_at > newest:
                    newest = created_at

    if newest is not None:
        lag = (dt.datetime.now(dt.timezone.utc) - newest).total_seconds()
        emit_metric("IngestLagSeconds", lag, "Seconds")
    rl = client.rate_limit_snapshot().get("core")
    return {"queued": queued, "rate_limit_remaining": rl.remaining if rl else None}
