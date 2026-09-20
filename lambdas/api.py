"""api: POST /triage via a Lambda Function URL.

Body: {"repo": "owner/name", "job_id": 123}          -- a job already ingested
   or {"repo": "owner/name", "log_text": "..."}       -- a raw log to triage ad hoc

Path per request: DynamoDB point lookup for the signature (hot path),
falling back to normalizing the supplied log; tsvector retrieval of
prior failures in Postgres (repo + recency filters in the statement);
one model call through the prompt-hash cache; citation resolved against
the neighbors actually shown. Emits TriageCostUSD and TriageLatencyMs.
"""
from __future__ import annotations

import base64
import json
import os
import time

import boto3

from common import anthropic_key_into_env, configure_db_env, emit_metric
from winnow.db import get_conn
from winnow.normalize import extract_signature
from winnow.retrieval import FullTextRetriever
from winnow.triage import SYSTEM_PROMPT, AnthropicTriageClient, cached_call, cost_usd, load_neighbors, render_user_prompt

MODEL = os.environ.get("WINNOW_TRIAGE_MODEL", "claude-opus-5")
K = int(os.environ.get("WINNOW_TRIAGE_K", "5"))

_ddb = boto3.resource("dynamodb")
_retriever = FullTextRetriever()
_client = None


def _respond(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def _signature_for(conn, repo_id: int, body: dict):
    """Returns (job_id or None, retrieval_text, nodeids, failure_key)."""
    if "job_id" in body:
        item = _ddb.Table(os.environ["WINNOW_SIGNATURE_TABLE"]).get_item(Key={"job_id": int(body["job_id"])}).get("Item")
        if item:
            return int(body["job_id"]), item["retrieval_text"], list(item.get("test_nodeids", [])), item.get("failure_key") or None
        with conn.cursor() as cur:
            cur.execute("SELECT retrieval_text, test_nodeids, failure_key FROM failure_signatures WHERE job_id = %s", (int(body["job_id"]),))
            row = cur.fetchone()
        if row:
            return int(body["job_id"]), row[0], list(row[1] or []), row[2]
        return None, None, None, None
    sig = extract_signature(body["log_text"])
    return None, sig.retrieval_text, sig.test_nodeids, sig.failure_key


def handler(event, context):
    global _client
    configure_db_env()
    anthropic_key_into_env()
    if _client is None:
        _client = AnthropicTriageClient(model=MODEL, effort="low")

    try:
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        body = json.loads(raw)
        owner, name = body["repo"].split("/", 1)
    except (KeyError, ValueError) as exc:
        return _respond(400, {"error": f"bad request: {exc}"})
    if "job_id" not in body and "log_text" not in body:
        return _respond(400, {"error": "provide job_id or log_text"})

    t0 = time.perf_counter()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM repos WHERE owner = %s AND name = %s", (owner, name))
            row = cur.fetchone()
        if not row:
            return _respond(404, {"error": f"unknown repo {body['repo']}"})
        repo_id = row[0]

        job_id, text, nodeids, key = _signature_for(conn, repo_id, body)
        if text is None:
            return _respond(404, {"error": f"job {body.get('job_id')} has no signature"})

        before = job_id if job_id is not None else 2**62
        neighbor_ids = _retriever.search(conn, repo_id, before, text, K)
        neighbors = load_neighbors(conn, neighbor_ids)
        t_retrieval = (time.perf_counter() - t0) * 1000

        call = cached_call(conn, _client, SYSTEM_PROMPT, render_user_prompt(text, nodeids, neighbors))
        v = call.verdict
        shown = {n.run_id: n for n in neighbors}
        citation = "none" if v.cited_run_id is None else ("supported" if v.cited_run_id in shown else "unresolved")
        if citation == "supported" and key and shown[v.cited_run_id] and key == _key_of(conn, shown[v.cited_run_id].job_id):
            citation = "supported_same_key"

    total_ms = (time.perf_counter() - t0) * 1000
    cost = cost_usd(MODEL, call.input_tokens, call.output_tokens, call.cache_read_tokens)
    emit_metric("TriageCostUSD", cost, "None", model=MODEL)
    emit_metric("TriageLatencyMs", total_ms, "Milliseconds", model=MODEL)
    return _respond(200, {
        "classification": v.classification, "confidence": v.confidence, "rationale": v.rationale,
        "cited_run_id": v.cited_run_id, "citation": citation,
        "neighbors": [{"job_id": n.job_id, "run_id": n.run_id, "label": n.label} for n in neighbors],
        "model": MODEL, "cached": call.cached, "cost_usd": round(cost, 6),
        "latency_ms": {"retrieval": round(t_retrieval, 1), "generation": round(call.latency_ms, 1), "total": round(total_ms, 1)},
    })


def _key_of(conn, job_id: int):
    with conn.cursor() as cur:
        cur.execute("SELECT failure_key FROM failure_signatures WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
    return row[0] if row else None
