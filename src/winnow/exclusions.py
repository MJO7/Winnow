"""Central registry of every reason a job/run is dropped before labeling.

Every rule here must show up, with a count, in the Phase 1 exit report.
Adding a new exclusion path without adding it here is how a labeling
pipeline quietly becomes unauditable -- so exclusions are only ever
recorded through `record()`, never as an ad hoc DB insert elsewhere.
"""
from __future__ import annotations

import psycopg


class Rule:
    NON_PUSH_OR_NON_DEFAULT_BRANCH = "non_push_or_non_default_branch"
    RUN_NOT_COMPLETED = "run_not_completed"
    RUN_CANCELLED_SUPERSEDED = "run_cancelled_superseded"
    NO_RESOLUTION_WITHIN_INGEST_WINDOW = "no_resolution_within_ingest_window"
    FORCE_PUSH_OR_DIVERGED_HISTORY = "force_push_or_diverged_history"
    WORKFLOW_AUTORETRY_SUSPECTED = "workflow_autoretry_suspected"

    ALL = (
        NON_PUSH_OR_NON_DEFAULT_BRANCH,
        RUN_NOT_COMPLETED,
        RUN_CANCELLED_SUPERSEDED,
        NO_RESOLUTION_WITHIN_INGEST_WINDOW,
        FORCE_PUSH_OR_DIVERGED_HISTORY,
        WORKFLOW_AUTORETRY_SUSPECTED,
    )


def record(
    conn: psycopg.Connection,
    repo_id: int,
    scope: str,
    scope_id: str,
    rule: str,
    detail: str | None = None,
) -> None:
    assert rule in Rule.ALL, f"unregistered exclusion rule: {rule!r} -- add it to winnow.exclusions.Rule"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO exclusions (repo_id, scope, scope_id, rule, detail) VALUES (%s, %s, %s, %s, %s)",
            (repo_id, scope, str(scope_id), rule, detail),
        )


def tally(conn: psycopg.Connection, repo_id: int | None = None) -> list[tuple[str, int]]:
    with conn.cursor() as cur:
        if repo_id is None:
            cur.execute("SELECT rule, COUNT(*) FROM exclusions GROUP BY rule ORDER BY 2 DESC")
        else:
            cur.execute(
                "SELECT rule, COUNT(*) FROM exclusions WHERE repo_id = %s GROUP BY rule ORDER BY 2 DESC",
                (repo_id,),
            )
        return cur.fetchall()
