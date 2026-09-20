"""In-process retry detection (contaminating case 3).

pytest-rerunfailures, `--reruns N`, flaky-decorator plugins, and retry
wrapper actions re-run a failing test *inside* one job. The job then
reports success and the flake never enters our dataset. That is
survivorship bias against the easiest flakes, and it can't be turned into
a per-job exclusion because the evidence was never emitted. What CAN be
done: grep each repo's workflow YAML for the markers, record what was
found, and state the bias direction in the write-up.

Workflow file bodies come from raw.githubusercontent.com download_urls,
which don't count against the REST rate limit; only the directory
listing does (one request per repo).
"""
from __future__ import annotations

import logging
import re

import psycopg

from winnow.github_client import GitHubClient
from winnow.repos import RepoSpec

logger = logging.getLogger("winnow.retry_scan")

RETRY_MARKERS: list[tuple[str, re.Pattern]] = [
    ("pytest --reruns", re.compile(r"--reruns\b")),
    ("pytest-rerunfailures", re.compile(r"pytest-rerunfailures|pytest_rerunfailures")),
    ("flaky plugin", re.compile(r"\bpytest-flaky\b|\bflaky\s*==|\b@flaky\b")),
    ("nick-fields/retry action", re.compile(r"nick-fields/retry|nick-invision/retry")),
    ("generic retry step", re.compile(r"\bretry[-_]?(count|times|on|attempts)\b", re.IGNORECASE)),
    ("max-attempts", re.compile(r"\bmax[-_]attempts\b", re.IGNORECASE)),
    ("unittest retry", re.compile(r"\bretry_on_connect_failures\b|\bretry_on_failure\b")),
]


def scan_repo(client: GitHubClient, conn: psycopg.Connection, repo_id: int, spec: RepoSpec) -> int:
    findings = 0
    with conn.cursor() as cur:
        cur.execute("DELETE FROM retry_config_findings WHERE repo_id = %s", (repo_id,))

    for wf in client.list_repo_workflow_files(spec.owner, spec.name):
        text = client.get_file_text(wf["download_url"])
        for lineno, line in enumerate(text.splitlines(), 1):
            for marker_name, pattern in RETRY_MARKERS:
                if pattern.search(line):
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO retry_config_findings (repo_id, workflow_path, marker, context_line)
                            VALUES (%s, %s, %s, %s)
                            """,
                            (repo_id, wf["path"], marker_name, f"L{lineno}: {line.strip()[:200]}"),
                        )
                    findings += 1
    conn.commit()
    logger.info("repo=%s retry-config findings=%d", spec.full_name, findings)
    return findings


def summarize(conn: psycopg.Connection, repo_id: int) -> list[tuple[str, int]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT marker, COUNT(*) FROM retry_config_findings WHERE repo_id = %s GROUP BY marker ORDER BY 2 DESC",
            (repo_id,),
        )
        return cur.fetchall()
