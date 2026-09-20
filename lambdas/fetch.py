"""fetch: one SQS message = one run. Pull jobs + steps into Postgres;
for each failed job, download the log, gzip it to S3, extract the
signature, write it to DynamoDB (point-lookup hot path, TTL 90d) and to
Postgres failure_signatures (the relational/retrieval index).

Partial-batch failure reporting means a transient error on one run
returns just that message to the queue; after 3 tries it lands in the
DLQ and the alarm fires.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import time

import boto3
from psycopg.types.json import Json

from common import configure_db_env, emit_metric, github_token
from winnow.db import get_conn
from winnow.github_client import GitHubClient, LogsUnavailable
from winnow.ingest import _fetch_jobs_for_run
from winnow.normalize import extract_signature

_s3 = boto3.client("s3")
_ddb = boto3.resource("dynamodb")
TTL_DAYS = 90


def _process_run(conn, client, owner, repo, repo_id, run_id) -> dict:
    n_jobs = _fetch_jobs_for_run(client, conn, owner, repo, repo_id, run_id)
    conn.commit()

    # Failed jobs needing a signature: never fetched, OR fetched to S3 but
    # the signature row was deleted (a normalizer change being replayed).
    # The second case reads S3, not GitHub -- the archive outlives
    # GitHub's log retention, which is the reason it exists.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.job_id, j.log_fetch_status, j.log_local_path FROM jobs j
            WHERE j.run_id = %s AND j.conclusion = 'failure'
              AND (j.log_fetch_status = 'not_attempted'
                   OR (j.log_fetch_status = 'fetched'
                       AND NOT EXISTS (SELECT 1 FROM failure_signatures fs WHERE fs.job_id = j.job_id)))
            """,
            (run_id,),
        )
        todo = cur.fetchall()

    table = _ddb.Table(os.environ["WINNOW_SIGNATURE_TABLE"])
    bucket = os.environ["WINNOW_LOG_BUCKET"]
    stats = {"jobs": n_jobs, "failed": len(todo), "logs": 0, "replayed": 0, "expired": 0}
    for job_id, status, path in todo:
        key = f"logs/{owner}/{repo}/{job_id}.log.gz"
        if status == "fetched" and path and path.startswith("s3://"):
            key = path.split("/", 3)[3]
            text = gzip.decompress(_s3.get_object(Bucket=bucket, Key=key)["Body"].read()).decode("utf-8", "replace")
            stats["replayed"] += 1
        else:
            try:
                text = client.get_job_logs_text(owner, repo, job_id)
            except LogsUnavailable as exc:
                status = "expired_410" if exc.status_code == 410 else "error"
                stats["expired"] += status == "expired_410"
                with conn.cursor() as cur:
                    cur.execute("UPDATE jobs SET log_fetch_status = %s WHERE job_id = %s", (status, job_id))
                continue
            _s3.put_object(Bucket=bucket, Key=key, Body=gzip.compress(text.encode("utf-8", "replace")), ContentType="text/plain", ContentEncoding="gzip")

        sig = extract_signature(text)
        expires_at = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=TTL_DAYS)).timestamp())
        table.put_item(Item={
            "job_id": job_id, "run_id": run_id, "repo": f"{owner}/{repo}", "s3_key": key,
            "signature_source": sig.signature_source, "exception_type": sig.exception_type or "",
            "retrieval_text": sig.retrieval_text, "failure_key": sig.failure_key or "",
            "signature_hash": sig.signature_hash, "test_nodeids": sig.test_nodeids[:20],
            "expires_at": expires_at,
        })

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET log_fetch_status = 'fetched', log_local_path = %s WHERE job_id = %s",
                (f"s3://{bucket}/{key}", job_id),
            )
            cur.execute(
                """
                INSERT INTO failure_signatures (job_id, signature_source, exception_type, message_skeleton,
                    top_frames, test_nodeids, signature_text, signature_hash, retrieval_text, failure_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (job_id) DO NOTHING
                """,
                (job_id, sig.signature_source, sig.exception_type, sig.message_skeleton,
                 Json([f.as_dict() for f in sig.top_frames]), Json(sig.test_nodeids),
                 sig.signature_text, sig.signature_hash, sig.retrieval_text, sig.failure_key),
            )
        conn.commit()
        stats["logs"] += 1
    return stats


def handler(event, context):
    configure_db_env()
    client = GitHubClient(token=github_token())
    failures = []
    with get_conn() as conn:
        for record in event.get("Records", []):
            body = json.loads(record["body"])
            t0 = time.perf_counter()
            try:
                stats = _process_run(conn, client, body["owner"], body["repo"], body["repo_id"], body["run_id"])
                print(json.dumps({"run_id": body["run_id"], **stats, "ms": round((time.perf_counter() - t0) * 1000)}))
                emit_metric("RunsFetched", 1, "Count")
            except Exception as exc:  # noqa: BLE001 - report to SQS, don't crash the batch
                conn.rollback()
                print(json.dumps({"run_id": body.get("run_id"), "error": repr(exc)}))
                failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
