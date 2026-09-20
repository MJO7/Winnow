from __future__ import annotations

import datetime as dt

from winnow.label import label_repo
from winnow.repos import RepoSpec
from tests.conftest import insert_run, insert_job, insert_step

SPEC = RepoSpec(owner="acme", name="widgets", default_branch="main", has_external_flaky_signal=False, notes="")


class FakeGitHubClient:
    """Stubs only compare_commits -- the only network call label.py makes."""

    def __init__(self, compare_result: str = "ahead"):
        self.token = "fake"
        self.compare_result = compare_result
        self.calls = []

    def compare_commits(self, owner, repo, base, head):
        self.calls.append((base, head))
        return {"status": self.compare_result}


def _labels(conn) -> dict[int, tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT job_id, label, label_reason FROM labels")
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def _exclusion_rules_for_job(conn, job_id: int) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT rule FROM exclusions WHERE scope = 'job' AND scope_id = %s", (str(job_id),))
        return [r[0] for r in cur.fetchall()]


def test_fail_then_succeed_same_run_is_flake(conn, repo_id):
    insert_run(conn, repo_id, run_id=1)
    insert_job(conn, repo_id, job_id=101, run_id=1, run_attempt=1, conclusion="failure")
    insert_job(conn, repo_id, job_id=102, run_id=1, run_attempt=2, conclusion="success")

    label_repo(FakeGitHubClient(), conn, repo_id, SPEC)

    labels = _labels(conn)
    assert labels[101] == ("flake", "rerun_same_run_id_succeeded")
    assert 102 not in labels  # the succeeding attempt isn't itself a labeled failure


def test_fail_fail_pass_on_third_attempt_labels_both_failures_flake(conn, repo_id):
    insert_run(conn, repo_id, run_id=1)
    insert_job(conn, repo_id, job_id=101, run_id=1, run_attempt=1, conclusion="failure")
    insert_job(conn, repo_id, job_id=102, run_id=1, run_attempt=2, conclusion="failure")
    insert_job(conn, repo_id, job_id=103, run_id=1, run_attempt=3, conclusion="success")

    label_repo(FakeGitHubClient(), conn, repo_id, SPEC)

    labels = _labels(conn)
    assert labels[101][0] == "flake"
    assert labels[102][0] == "flake"
    assert 103 not in labels


def test_never_succeeds_but_later_push_fixes_it_is_real(conn, repo_id):
    insert_run(conn, repo_id, run_id=1, head_sha="sha_bad", created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    insert_job(conn, repo_id, job_id=101, run_id=1, run_attempt=1, conclusion="failure")

    insert_run(conn, repo_id, run_id=2, head_sha="sha_fixed", created_at=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc))
    insert_job(conn, repo_id, job_id=201, run_id=2, run_attempt=1, conclusion="success")

    client = FakeGitHubClient(compare_result="ahead")
    label_repo(client, conn, repo_id, SPEC)

    labels = _labels(conn)
    assert labels[101][0] == "real"
    assert client.calls == [("sha_bad", "sha_fixed")]


def test_diverged_history_is_excluded_not_labeled_real(conn, repo_id):
    insert_run(conn, repo_id, run_id=1, head_sha="sha_bad", created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    insert_job(conn, repo_id, job_id=101, run_id=1, run_attempt=1, conclusion="failure")

    insert_run(conn, repo_id, run_id=2, head_sha="sha_rewritten", created_at=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc))
    insert_job(conn, repo_id, job_id=201, run_id=2, run_attempt=1, conclusion="success")

    client = FakeGitHubClient(compare_result="diverged")
    label_repo(client, conn, repo_id, SPEC)

    labels = _labels(conn)
    assert 101 not in labels
    assert "force_push_or_diverged_history" in _exclusion_rules_for_job(conn, 101)


def test_no_later_success_is_excluded_as_unresolved(conn, repo_id):
    insert_run(conn, repo_id, run_id=1, head_sha="sha_bad")
    insert_job(conn, repo_id, job_id=101, run_id=1, run_attempt=1, conclusion="failure")

    label_repo(FakeGitHubClient(), conn, repo_id, SPEC)

    labels = _labels(conn)
    assert 101 not in labels
    assert "no_resolution_within_ingest_window" in _exclusion_rules_for_job(conn, 101)


def test_infra_setup_step_failure_labeled_infra_not_real(conn, repo_id):
    insert_run(conn, repo_id, run_id=1, head_sha="sha_bad")
    insert_job(conn, repo_id, job_id=101, run_id=1, run_attempt=1, conclusion="failure")
    insert_step(conn, job_id=101, name="Set up job", conclusion="failure", number=1)
    insert_step(conn, job_id=101, name="Run tests", conclusion="cancelled", number=2)

    client = FakeGitHubClient()
    label_repo(client, conn, repo_id, SPEC)

    labels = _labels(conn)
    assert labels[101] == ("infra", "infra_setup_step_failed")
    assert client.calls == []  # never even attempted forward resolution


def test_pull_request_event_excluded_not_labeled(conn, repo_id):
    insert_run(conn, repo_id, run_id=1, event="pull_request", head_branch="feature-x")
    insert_job(conn, repo_id, job_id=101, run_id=1, run_attempt=1, conclusion="failure")

    label_repo(FakeGitHubClient(), conn, repo_id, SPEC)

    labels = _labels(conn)
    assert 101 not in labels
    with conn.cursor() as cur:
        cur.execute("SELECT rule FROM exclusions WHERE scope = 'run' AND scope_id = '1'")
        assert cur.fetchone()[0] == "non_push_or_non_default_branch"


def test_non_default_branch_push_excluded(conn, repo_id):
    insert_run(conn, repo_id, run_id=1, event="push", head_branch="release-1.0")
    insert_job(conn, repo_id, job_id=101, run_id=1, run_attempt=1, conclusion="failure")

    label_repo(FakeGitHubClient(), conn, repo_id, SPEC)

    assert 101 not in _labels(conn)


def test_cancelled_run_excluded_as_superseded(conn, repo_id):
    insert_run(conn, repo_id, run_id=1, conclusion="cancelled")
    insert_job(conn, repo_id, job_id=101, run_id=1, run_attempt=1, conclusion="cancelled")

    label_repo(FakeGitHubClient(), conn, repo_id, SPEC)

    assert 101 not in _labels(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT rule FROM exclusions WHERE scope = 'run' AND scope_id = '1'")
        assert cur.fetchone()[0] == "run_cancelled_superseded"


def test_autoretry_workflow_detected_and_excluded_instead_of_flake(conn, repo_id):
    # 5+ run groups, all with a tight ~60s delay between attempt1 completion
    # and attempt2 start -- the bot-auto-retry signature.
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    for i in range(6):
        run_id = 100 + i
        insert_run(conn, repo_id, run_id=run_id, workflow_id=9, created_at=base + dt.timedelta(hours=i))
        c1 = base + dt.timedelta(hours=i, minutes=5)
        s2 = base + dt.timedelta(hours=i, minutes=5, seconds=61)  # ~61s later, every time
        insert_job(conn, repo_id, job_id=1000 + i * 2, run_id=run_id, run_attempt=1, conclusion="failure", completed_at=c1)
        insert_job(conn, repo_id, job_id=1000 + i * 2 + 1, run_id=run_id, run_attempt=2, conclusion="success", started_at=s2)

    label_repo(FakeGitHubClient(), conn, repo_id, SPEC)

    labels = _labels(conn)
    assert not labels  # none of these should be labeled 'flake' -- workflow is auto-retry-suspected
    assert "workflow_autoretry_suspected" in _exclusion_rules_for_job(conn, 1000)


def test_manual_human_rerun_with_high_variance_delay_is_not_flagged_autoretry(conn, repo_id):
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    delays_minutes = [5, 240, 30, 1440, 15, 90]  # minutes to a full day -- human territory
    for i, delay_min in enumerate(delays_minutes):
        run_id = 200 + i
        insert_run(conn, repo_id, run_id=run_id, workflow_id=10, created_at=base + dt.timedelta(hours=i))
        c1 = base + dt.timedelta(hours=i, minutes=5)
        s2 = c1 + dt.timedelta(minutes=delay_min)
        insert_job(conn, repo_id, job_id=2000 + i * 2, run_id=run_id, run_attempt=1, conclusion="failure", completed_at=c1)
        insert_job(conn, repo_id, job_id=2000 + i * 2 + 1, run_id=run_id, run_attempt=2, conclusion="success", started_at=s2)

    label_repo(FakeGitHubClient(), conn, repo_id, SPEC)

    labels = _labels(conn)
    assert labels[2000][0] == "flake"


def test_rerunning_labeler_is_idempotent(conn, repo_id):
    insert_run(conn, repo_id, run_id=1)
    insert_job(conn, repo_id, job_id=101, run_id=1, run_attempt=1, conclusion="failure")
    insert_job(conn, repo_id, job_id=102, run_id=1, run_attempt=2, conclusion="success")

    label_repo(FakeGitHubClient(), conn, repo_id, SPEC)
    label_repo(FakeGitHubClient(), conn, repo_id, SPEC)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM labels WHERE job_id = 101")
        assert cur.fetchone()[0] == 1
