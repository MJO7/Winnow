"""Minimal forward-only migration runner: applies migrations/*.sql in
filename order, tracking what's been applied in schema_migrations. No
rollback support -- for a solo project at this stage, forward-only is the
right amount of machinery. Reach for Alembic if this ever needs branching.
"""
from __future__ import annotations

from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"


def apply_migrations(database_url: str, quiet: bool = False) -> int:
    def say(msg: str) -> None:
        if not quiet:
            print(msg)

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("SELECT filename FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}
        conn.commit()

        pending = sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.name not in applied)
        for path in pending:
            say(f"Applying {path.name} ...")
            with conn.cursor() as cur:
                cur.execute(path.read_text())
                cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            conn.commit()
            say(f"  OK: {path.name}")

    return len(pending)
