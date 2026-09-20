from __future__ import annotations

import time

import pytest
import responses

from winnow.github_client import GitHubClient, GitHubAPIError, LogsUnavailable


def _rl_headers(remaining: int, limit: int = 5000, reset_in: int = 3600) -> dict:
    return {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(int(time.time()) + reset_in),
    }


@responses.activate
def test_paginate_follows_link_header():
    client = GitHubClient(token="t")
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r/actions/runs",
        json={"workflow_runs": [{"id": 1}, {"id": 2}]},
        headers={**_rl_headers(4998), "Link": '<https://api.github.com/repos/o/r/actions/runs?page=2>; rel="next"'},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r/actions/runs?page=2",
        json={"workflow_runs": [{"id": 3}]},
        headers=_rl_headers(4997),
        status=200,
    )

    items = list(client.paginate("/repos/o/r/actions/runs", items_key="workflow_runs"))
    assert [i["id"] for i in items] == [1, 2, 3]


@responses.activate
def test_retries_on_500_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    client = GitHubClient(token="t")
    responses.add(responses.GET, "https://api.github.com/repos/o/r", status=500)
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r",
        json={"id": 1, "default_branch": "main"},
        headers=_rl_headers(4999),
        status=200,
    )

    body = client.get_json("/repos/o/r")
    assert body["default_branch"] == "main"
    assert len(responses.calls) == 2


@responses.activate
def test_secondary_rate_limit_respects_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    client = GitHubClient(token="t")
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r",
        json={"message": "You have exceeded a secondary rate limit"},
        headers={"Retry-After": "2"},
        status=403,
    )
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r",
        json={"id": 1},
        headers=_rl_headers(4999),
        status=200,
    )

    client.get_json("/repos/o/r")
    assert slept == [2.0]


@responses.activate
def test_primary_rate_limit_waits_for_reset(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    client = GitHubClient(token="t")
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r",
        json={"message": "API rate limit exceeded for user"},
        headers=_rl_headers(0, reset_in=10),
        status=403,
    )
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r",
        json={"id": 1},
        headers=_rl_headers(4999),
        status=200,
    )

    client.get_json("/repos/o/r")
    assert len(slept) == 1
    assert 9 <= slept[0] <= 11


def test_search_and_core_rate_limits_tracked_separately():
    client = GitHubClient(token="t")
    import requests

    core_resp = requests.Response()
    core_resp.headers.update(_rl_headers(4990, limit=5000))
    search_resp = requests.Response()
    search_resp.headers.update(_rl_headers(5, limit=30))

    client._update_rate_limit("https://api.github.com/repos/o/r", core_resp)
    client._update_rate_limit("https://api.github.com/search/issues?q=x", search_resp)

    snap = client.rate_limit_snapshot()
    assert snap["core"].remaining == 4990
    assert snap["search"].remaining == 5
    # Throttling on a core URL must not be triggered by search's low count.
    client._throttle_if_needed("https://api.github.com/repos/o/r")  # should not sleep/raise


def test_throttle_watermark_scales_with_limit(monkeypatch):
    """Regression: an absolute watermark of 50 throttled after ~10 requests
    against the 60/hr unauthenticated budget and slept for 53 minutes."""
    import requests

    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    client = GitHubClient(token=None)

    resp = requests.Response()
    resp.headers.update(_rl_headers(remaining=40, limit=60, reset_in=100))
    client._update_rate_limit("https://api.github.com/repos/o/r", resp)
    client._throttle_if_needed("https://api.github.com/repos/o/r")
    assert slept == [], "40/60 remaining must not throttle"

    resp.headers.update(_rl_headers(remaining=2, limit=60, reset_in=100))
    client._update_rate_limit("https://api.github.com/repos/o/r", resp)
    client._throttle_if_needed("https://api.github.com/repos/o/r")
    assert len(slept) == 1, "2/60 remaining must throttle"

    slept.clear()
    resp.headers.update(_rl_headers(remaining=80, limit=5000, reset_in=100))
    client._update_rate_limit("https://api.github.com/repos/o/r", resp)
    client._throttle_if_needed("https://api.github.com/repos/o/r")
    assert len(slept) == 1, "80/5000 is below the 2% watermark (100) and must throttle"


@responses.activate
def test_job_logs_without_token_raises_logs_unavailable():
    client = GitHubClient(token=None)
    with pytest.raises(LogsUnavailable, match="no_token"):
        client.get_job_logs_text("o", "r", 123)
    assert len(responses.calls) == 0  # never even makes the request


@responses.activate
def test_job_logs_expired_raises_410():
    client = GitHubClient(token="t")
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r/actions/jobs/123/logs",
        status=410,
        headers=_rl_headers(4999),
    )
    with pytest.raises(LogsUnavailable, match="expired_410"):
        client.get_job_logs_text("o", "r", 123)


@responses.activate
def test_get_json_raises_on_4xx():
    client = GitHubClient(token="t")
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r",
        json={"message": "Not Found"},
        headers=_rl_headers(4999),
        status=404,
    )
    with pytest.raises(GitHubAPIError):
        client.get_json("/repos/o/r")
