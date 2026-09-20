from __future__ import annotations

import datetime as dt
import os

import psycopg
import pytest

from winnow.config import load_config
from winnow.migrate import apply_migrations

TEST_DB_NAME = "winnow_test"

TABLES_IN_FK_ORDER = [
    "llm_cache",
    "signature_embeddings",
    "retry_config_findings",
    "external_flaky_signals",
    "exclusions",
    "labels",
    "failure_signatures",
    "steps",
    "jobs",
    "workflow_runs",
    "repos",
]


def _test_database_url() -> str:
    """Tests get their own database on the same local Postgres, never the
    dev one. Learned the hard way: the first version of this fixture
    truncated DATABASE_URL directly and wiped a smoke-ingest mid-session.
    """
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    dev_url = load_config().database_url
    base, _, _ = dev_url.rpartition("/")
    return f"{base}/{TEST_DB_NAME}"


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = _test_database_url()
    dev_url = load_config().database_url
    with psycopg.connect(dev_url, autocommit=True) as admin:
        with admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    apply_migrations(url, quiet=True)
    return url


@pytest.fixture()
def conn(test_database_url):
    """Connects to the dedicated test database on the local docker-compose
    Postgres and truncates all Winnow tables before the test runs. This is
    an integration fixture, not a unit-test one -- it requires
    `docker compose up -d` to have been run first, same as actually using
    the tool. Documented in README under "Running tests".
    """
    connection = psycopg.connect(test_database_url)
    with connection.cursor() as cur:
        cur.execute("TRUNCATE " + ", ".join(TABLES_IN_FK_ORDER) + " RESTART IDENTITY CASCADE")
    connection.commit()
    yield connection
    connection.rollback()
    connection.close()


@pytest.fixture()
def repo_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repos (owner, name, default_branch) VALUES ('acme', 'widgets', 'main') RETURNING id"
        )
        rid = cur.fetchone()[0]
    conn.commit()
    return rid


def insert_run(
    conn,
    repo_id: int,
    run_id: int,
    workflow_id: int = 1,
    event: str = "push",
    head_branch: str = "main",
    head_sha: str = "sha0000",
    status: str = "completed",
    conclusion: str = "failure",
    created_at: dt.datetime | None = None,
) -> None:
    created_at = created_at or dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflow_runs (
                run_id, repo_id, workflow_id, workflow_name, run_number, event,
                head_branch, head_sha, status, conclusion, latest_run_attempt,
                created_at
            ) VALUES (%s, %s, %s, 'CI', 1, %s, %s, %s, %s, %s, 1, %s)
            """,
            (run_id, repo_id, workflow_id, event, head_branch, head_sha, status, conclusion, created_at),
        )
    conn.commit()


def insert_job(
    conn,
    repo_id: int,
    job_id: int,
    run_id: int,
    name: str = "test",
    run_attempt: int = 1,
    conclusion: str = "failure",
    started_at: dt.datetime | None = None,
    completed_at: dt.datetime | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (job_id, run_id, repo_id, name, run_attempt, status, conclusion, started_at, completed_at)
            VALUES (%s, %s, %s, %s, %s, 'completed', %s, %s, %s)
            """,
            (job_id, run_id, repo_id, name, run_attempt, conclusion, started_at, completed_at),
        )
    conn.commit()


def insert_step(conn, job_id: int, name: str, conclusion: str, number: int = 1) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO steps (job_id, name, number, status, conclusion) VALUES (%s, %s, %s, 'completed', %s)",
            (job_id, name, number, conclusion),
        )
    conn.commit()
