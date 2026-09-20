"""Shared Lambda plumbing: secrets at cold start, DB URL into the
environment so winnow.db.get_conn() works unchanged, and CloudWatch
metrics via the embedded-metric-format log line (no PutMetricData call,
no extra latency on the request path).
"""
from __future__ import annotations

import json
import os
import time
from functools import lru_cache

import boto3

_secrets = boto3.client("secretsmanager")


@lru_cache(maxsize=8)
def secret(arn: str) -> str:
    return _secrets.get_secret_value(SecretId=arn)["SecretString"]


def configure_db_env() -> None:
    if "DATABASE_URL" in os.environ:
        return
    db = json.loads(secret(os.environ["WINNOW_DB_SECRET_ARN"]))
    os.environ["DATABASE_URL"] = db["url"]


def github_token() -> str:
    return secret(os.environ["WINNOW_GITHUB_SECRET_ARN"])


def anthropic_key_into_env() -> None:
    if "ANTHROPIC_API_KEY" not in os.environ:
        os.environ["ANTHROPIC_API_KEY"] = secret(os.environ["WINNOW_ANTHROPIC_SECRET_ARN"])


def repos() -> list[str]:
    return [r for r in os.environ.get("WINNOW_REPOS", "").split(",") if r]


def emit_metric(name: str, value: float, unit: str = "None", **dimensions: str) -> None:
    """CloudWatch Embedded Metric Format: one JSON log line, picked up
    asynchronously by CloudWatch Logs. Namespace 'Winnow' matches the
    IAM condition and the alarms."""
    dims = dict(dimensions)
    doc = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {"Namespace": "Winnow", "Dimensions": [list(dims.keys())] if dims else [[]],
                 "Metrics": [{"Name": name, "Unit": unit}]}
            ],
        },
        name: value,
        **dims,
    }
    print(json.dumps(doc))
