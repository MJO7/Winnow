"""Runs the normalizer over every fetched failed-job log that doesn't have
a signature row yet. Separate from ingest so a normalizer change means
`DELETE FROM failure_signatures` + rerun, never a re-download.
"""
from __future__ import annotations

import logging
from pathlib import Path

import psycopg
from psycopg.types.json import Json

from winnow.normalize import extract_signature

logger = logging.getLogger("winnow.signatures")


def build_signatures(conn: psycopg.Connection, repo_id: int) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.job_id, j.log_local_path
            FROM jobs j
            WHERE j.repo_id = %s
              AND j.log_fetch_status = 'fetched'
              AND NOT EXISTS (SELECT 1 FROM failure_signatures fs WHERE fs.job_id = j.job_id)
            """,
            (repo_id,),
        )
        rows = cur.fetchall()

    counts: dict[str, int] = {}
    for job_id, path in rows:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        sig = extract_signature(raw)
        counts[sig.signature_source] = counts.get(sig.signature_source, 0) + 1
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO failure_signatures (
                    job_id, signature_source, exception_type, message_skeleton,
                    top_frames, test_nodeids, signature_text, signature_hash
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    job_id,
                    sig.signature_source,
                    sig.exception_type,
                    sig.message_skeleton,
                    Json([f.as_dict() for f in sig.top_frames]),
                    Json(sig.test_nodeids),
                    sig.signature_text,
                    sig.signature_hash,
                ),
            )
    conn.commit()
    logger.info("repo_id=%s signatures built: %s", repo_id, counts)
    return counts
