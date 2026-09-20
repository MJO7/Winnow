"""External flakiness signal for label validation (Phase 1 exit criterion).

pytorch/pytorch's flaky-test bot auto-files issues titled
    DISABLED test_name (module.path.ClassName)
labeled `module: flaky-tests`. That's an independent, per-test flakiness
judgement made by a system that has nothing to do with Winnow's re-run
join -- which is exactly what makes it usable as a check on the join.

Agreement is measured per TEST, not per job: a job-level flake label says
"this CI job passed on re-run", and the job's parsed log tells us which
test node ids failed in it (failure_signatures.test_nodeids). A derived-
flaky test is one that appears in the failing-test set of at least one
job labeled flake. That set is intersected with the bot's set.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Json

from winnow.github_client import GitHubClient

logger = logging.getLogger("winnow.external_signals")

_DISABLED_TITLE = re.compile(r"^DISABLED\s+(\S+)\s+\(([\w.]+)\)\s*$")
_SEARCH_RESULT_CAP = 1000
_SEARCH_PAGE_SIZE = 100

PYTORCH_SOURCE = "pytorch_disabled_test_issue"


@dataclass
class DisabledTest:
    test_name: str
    test_class: str
    state: str
    url: str
    created_at: dt.datetime
    raw: dict


def _parse_issue(issue: dict) -> DisabledTest | None:
    m = _DISABLED_TITLE.match(issue.get("title", ""))
    if not m:
        return None
    return DisabledTest(
        test_name=m.group(1),
        test_class=m.group(2),
        state=issue.get("state", ""),
        url=issue.get("html_url", ""),
        created_at=dt.datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00")),
        raw=issue,
    )


def _count_for_window(client: GitHubClient, base_query: str, start: dt.date, end: dt.date) -> int:
    q = f"{base_query} created:{start.isoformat()}..{end.isoformat()}"
    body = client.get_json("/search/issues", params={"q": q, "per_page": 1})
    return int(body.get("total_count", 0))


def fetch_pytorch_disabled_tests(
    client: GitHubClient,
    since: dt.date,
    until: dt.date | None = None,
) -> list[DisabledTest]:
    """Walks `created:` date windows, halving any window that would exceed
    the search API's 1,000-result cap, so the full set is retrievable
    rather than silently truncated at the first thousand.
    """
    until = until or dt.date.today()
    base_query = 'repo:pytorch/pytorch is:issue label:"module: flaky-tests"'

    windows: list[tuple[dt.date, dt.date]] = [(since, until)]
    results: list[DisabledTest] = []
    seen_urls: set[str] = set()

    while windows:
        start, end = windows.pop()
        total = _count_for_window(client, base_query, start, end)
        if total > _SEARCH_RESULT_CAP and (end - start).days >= 1:
            mid = start + (end - start) / 2
            windows.append((start, mid))
            windows.append((mid + dt.timedelta(days=1), end))
            continue
        if total == 0:
            continue

        q = f"{base_query} created:{start.isoformat()}..{end.isoformat()}"
        n_window = 0
        for issue in client.paginate("/search/issues", params={"q": q, "per_page": _SEARCH_PAGE_SIZE}, items_key="items"):
            parsed = _parse_issue(issue)
            if parsed is None or parsed.url in seen_urls:
                continue
            seen_urls.add(parsed.url)
            results.append(parsed)
            n_window += 1
        logger.info("window %s..%s: %d/%d issues parsed as DISABLED tests", start, end, n_window, total)

    return results


def store_signals(conn: psycopg.Connection, repo_id: int, tests: list[DisabledTest], source: str) -> int:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM external_flaky_signals WHERE repo_id = %s AND source = %s", (repo_id, source))
        for t in tests:
            cur.execute(
                """
                INSERT INTO external_flaky_signals
                    (repo_id, test_class, test_name, source, source_url, state, reported_at, raw)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (repo_id, t.test_class, t.test_name, source, t.url, t.state, t.created_at, Json(t.raw)),
            )
    conn.commit()
    return len(tests)


@dataclass
class Agreement:
    external_flaky: int
    derived_flaky: int
    derived_real: int
    overlap_flaky: int          # derived flaky AND external flaky
    derived_flaky_not_external: int
    external_flaky_seen_but_derived_real: int
    external_flaky_never_observed: int

    @property
    def precision_of_derived_flake(self) -> float | None:
        """Of tests we call flaky, what fraction does the bot also call flaky?"""
        return self.overlap_flaky / self.derived_flaky if self.derived_flaky else None

    @property
    def recall_of_derived_flake(self) -> float | None:
        """Of bot-flaky tests we observed failing at all, what fraction did we call flaky?"""
        observed = self.overlap_flaky + self.external_flaky_seen_but_derived_real
        return self.overlap_flaky / observed if observed else None


def compute_agreement(conn: psycopg.Connection, repo_id: int, source: str = PYTORCH_SOURCE) -> Agreement:
    """Requires failure_signatures.test_nodeids -- i.e. fetched + parsed
    logs. With no logs, every derived set is empty and the result says so
    honestly rather than reporting 100% of nothing.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT test_name FROM external_flaky_signals WHERE repo_id = %s AND source = %s",
            (repo_id, source),
        )
        external = {r[0] for r in cur.fetchall()}

        cur.execute(
            """
            SELECT l.label, fs.test_nodeids
            FROM labels l
            JOIN failure_signatures fs ON fs.job_id = l.job_id
            WHERE l.repo_id = %s AND fs.test_nodeids IS NOT NULL
            """,
            (repo_id,),
        )
        derived_flaky: set[str] = set()
        derived_real: set[str] = set()
        for label, nodeids in cur.fetchall():
            names = {_test_name_from_nodeid(n) for n in (nodeids or [])}
            if label == "flake":
                derived_flaky |= names
            elif label == "real":
                derived_real |= names

    overlap = derived_flaky & external
    return Agreement(
        external_flaky=len(external),
        derived_flaky=len(derived_flaky),
        derived_real=len(derived_real),
        overlap_flaky=len(overlap),
        derived_flaky_not_external=len(derived_flaky - external),
        external_flaky_seen_but_derived_real=len((derived_real - derived_flaky) & external),
        external_flaky_never_observed=len(external - derived_flaky - derived_real),
    )


def _test_name_from_nodeid(nodeid: str) -> str:
    """'test/test_foo.py::TestBar::test_baz[param]' -> 'test_baz'. The bot's
    titles carry the bare method name plus class; parametrized ids are
    collapsed to the method since the bot disables at method granularity.
    """
    last = nodeid.split("::")[-1]
    return last.split("[", 1)[0]
