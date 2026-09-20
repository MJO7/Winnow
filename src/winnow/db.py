from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg

from winnow.config import load_config


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    cfg = load_config()
    conn = psycopg.connect(cfg.database_url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_or_create_repo(
    conn: psycopg.Connection,
    owner: str,
    name: str,
    default_branch: str,
    has_external_flaky_signal: bool = False,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM repos WHERE owner = %s AND name = %s", (owner, name)
        )
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            """
            INSERT INTO repos (owner, name, default_branch, has_external_flaky_signal)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (owner, name, default_branch, has_external_flaky_signal),
        )
        return cur.fetchone()[0]
