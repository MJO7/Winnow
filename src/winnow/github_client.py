from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Iterator

import requests

logger = logging.getLogger("winnow.github_client")

API_ROOT = "https://api.github.com"

# Stop consuming the primary rate limit budget when remaining drops to this
# fraction of the limit -- leaves headroom for other processes sharing the
# token and avoids the "wait until reset" cliff. A fraction, not an absolute:
# an absolute 50 is sane against 5000/hr but throttles after ten requests
# against the 60/hr unauthenticated budget (measured: it slept 53 minutes on
# the first smoke test).
LOW_WATERMARK_FRACTION = 0.02
LOW_WATERMARK_MIN = 3


class GitHubAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str, url: str):
        super().__init__(f"GitHub API {status_code} for {url}: {message}")
        self.status_code = status_code
        self.url = url


class LogsUnavailable(RuntimeError):
    """Raised for job-log fetches that fail for a reason that IS the
    answer (expired retention, no auth) rather than a transient error.
    Callers should catch this and record it, not retry.
    """

    def __init__(self, reason: str, status_code: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


@dataclass
class RateLimitStatus:
    limit: int
    remaining: int
    reset_epoch: int

    def seconds_until_reset(self) -> float:
        return max(0.0, self.reset_epoch - time.time())


class GitHubClient:
    """Thin wrapper around the GitHub REST API with:
      - a token-bucket-ish limiter driven by the X-RateLimit-* response
        headers (the real signal; a counted local budget would drift from
        reality the moment any other process shares the token)
      - exponential backoff with jitter on 5xx / network errors
      - honoring Retry-After on secondary rate limits (403 with that header)
      - Link-header pagination as a generator, so callers can bound how
        much they pull without the client guessing a page count
    """

    def __init__(self, token: str | None = None, session: requests.Session | None = None):
        self.token = token
        self.session = session or requests.Session()
        # GitHub buckets rate limits per resource (core, search, graphql, ...)
        # but stamps every response with headers scoped to whichever bucket
        # was hit. Search's bucket is tiny (30/min authenticated) compared to
        # core's 5000/hr, so tracking one shared status would make the
        # client think core is nearly exhausted after a single search call.
        self._rate_limits: dict[str, RateLimitStatus] = {}

    # -- low level -----------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "winnow-ci-triage (github.com/MJO7/winnow)",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _resource_for_url(url: str) -> str:
        return "search" if "/search/" in url else "core"

    def _throttle_if_needed(self, url: str) -> None:
        rl = self._rate_limits.get(self._resource_for_url(url))
        if rl is None:
            return
        watermark = max(LOW_WATERMARK_MIN, int(rl.limit * LOW_WATERMARK_FRACTION))
        if rl.remaining <= watermark:
            wait = rl.seconds_until_reset() + 1.0
            logger.warning(
                "Rate limit low (%d/%d remaining) -- sleeping %.0fs until reset",
                rl.remaining,
                rl.limit,
                wait,
            )
            time.sleep(wait)

    def _update_rate_limit(self, url: str, resp: requests.Response) -> None:
        try:
            limit = int(resp.headers.get("X-RateLimit-Limit", 0))
            remaining = int(resp.headers.get("X-RateLimit-Remaining", 0))
            reset_epoch = int(resp.headers.get("X-RateLimit-Reset", 0))
        except ValueError:
            return
        if limit:
            self._rate_limits[self._resource_for_url(url)] = RateLimitStatus(
                limit, remaining, reset_epoch
            )

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        max_retries: int = 5,
        allow_redirects: bool = True,
    ) -> requests.Response:
        """One retried, rate-limit-aware request. `url` may be absolute
        (as Link headers give us) or root-relative.
        """
        if not url.startswith("http"):
            url = f"{API_ROOT}{url}"

        self._throttle_if_needed(url)

        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.session.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    timeout=30,
                    allow_redirects=allow_redirects,
                )
            except requests.RequestException as exc:
                if attempt > max_retries:
                    raise
                delay = self._backoff_delay(attempt)
                logger.warning("Network error (%s), retrying in %.1fs: %s", attempt, delay, exc)
                time.sleep(delay)
                continue

            self._update_rate_limit(url, resp)

            if resp.status_code == 403 and self._is_secondary_rate_limit(resp):
                retry_after = float(resp.headers.get("Retry-After", 5))
                logger.warning("Secondary rate limit hit, sleeping %.0fs", retry_after)
                time.sleep(retry_after)
                continue

            if resp.status_code == 403 and self._is_primary_rate_limit(resp):
                rl = self._rate_limits.get(self._resource_for_url(url))
                wait = (rl.seconds_until_reset() + 1.0) if rl else 60.0
                logger.warning("Primary rate limit exhausted, sleeping %.0fs", wait)
                time.sleep(wait)
                continue

            if resp.status_code >= 500:
                if attempt > max_retries:
                    resp.raise_for_status()
                delay = self._backoff_delay(attempt)
                logger.warning("Server error %d, retrying in %.1fs", resp.status_code, delay)
                time.sleep(delay)
                continue

            return resp

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        base = min(2 ** attempt, 60)
        return base + random.uniform(0, base * 0.25)

    @staticmethod
    def _is_secondary_rate_limit(resp: requests.Response) -> bool:
        return "retry-after" in {k.lower() for k in resp.headers.keys()}

    @staticmethod
    def _is_primary_rate_limit(resp: requests.Response) -> bool:
        try:
            body = resp.json()
        except ValueError:
            return False
        msg = str(body.get("message", "")).lower()
        return "rate limit" in msg and "secondary" not in msg

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self.request("GET", url, params=params)
        if resp.status_code >= 400:
            raise GitHubAPIError(resp.status_code, resp.text[:500], url)
        return resp.json()

    def paginate(
        self, url: str, params: dict[str, Any] | None = None, items_key: str | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yields individual items across all pages, following the Link
        header rather than incrementing `page` blindly -- correct even if
        GitHub changes page size or the result set shifts mid-walk.
        """
        params = dict(params or {})
        params.setdefault("per_page", 100)
        next_url: str | None = url
        next_params: dict[str, Any] | None = params

        while next_url:
            resp = self.request("GET", next_url, params=next_params)
            if resp.status_code >= 400:
                raise GitHubAPIError(resp.status_code, resp.text[:500], next_url)
            body = resp.json()
            items = body[items_key] if items_key else body
            for item in items:
                yield item

            next_url = resp.links.get("next", {}).get("url")
            next_params = None  # next_url already carries the query string

    # -- high level ------------------------------------------------------

    def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return self.get_json(f"/repos/{owner}/{repo}")

    def rate_limit_snapshot(self) -> dict[str, RateLimitStatus]:
        return dict(self._rate_limits)

    def list_workflow_runs(
        self,
        owner: str,
        repo: str,
        event: str | None = None,
        branch: str | None = None,
        created: str | None = None,
        status: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {}
        if event:
            params["event"] = event
        if branch:
            params["branch"] = branch
        if created:
            params["created"] = created
        if status:
            params["status"] = status
        yield from self.paginate(
            f"/repos/{owner}/{repo}/actions/runs", params=params, items_key="workflow_runs"
        )

    def list_jobs_for_run(
        self, owner: str, repo: str, run_id: int, all_attempts: bool = True
    ) -> Iterator[dict[str, Any]]:
        params = {"filter": "all" if all_attempts else "latest"}
        yield from self.paginate(
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
            params=params,
            items_key="jobs",
        )

    def get_job_logs_text(self, owner: str, repo: str, job_id: int) -> str:
        """Downloads and decodes a job's log. Requires auth even for
        public repos -- GitHub returns 403 "Must have admin rights" for
        unauthenticated requests to this endpoint regardless of repo
        visibility, which is not really about admin rights at all.
        """
        if not self.token:
            raise LogsUnavailable("no_token: log downloads require GITHUB_TOKEN")

        resp = self.request("GET", f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs")
        if resp.status_code == 410:
            raise LogsUnavailable("expired_410: log retention window has passed", 410)
        if resp.status_code == 404:
            raise LogsUnavailable("not_found_404", 404)
        if resp.status_code == 403:
            raise LogsUnavailable("forbidden_403", 403)
        if resp.status_code >= 400:
            raise GitHubAPIError(resp.status_code, resp.text[:500], resp.url or "")
        return resp.text

    def compare_commits(self, owner: str, repo: str, base: str, head: str) -> dict[str, Any]:
        """GET .../compare/{base}...{head}. `status` is 'ahead', 'behind',
        'identical', or 'diverged'. Used to confirm a later "fixing" push
        is actually a descendant of the failing commit, not a force-push
        history rewrite (contaminating case 1).
        """
        return self.get_json(f"/repos/{owner}/{repo}/compare/{base}...{head}")

    def list_repo_workflow_files(self, owner: str, repo: str) -> Iterator[dict[str, Any]]:
        try:
            items = self.get_json(f"/repos/{owner}/{repo}/contents/.github/workflows")
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                return
            raise
        for item in items:
            if item.get("type") == "file" and item["name"].endswith((".yml", ".yaml")):
                yield item

    def get_file_text(self, download_url: str) -> str:
        resp = self.request("GET", download_url)
        resp.raise_for_status()
        return resp.text

    def search_issues(self, query: str) -> Iterator[dict[str, Any]]:
        """Search API has its own, much tighter rate limit (30 req/min
        authenticated, 10/min unauthenticated) -- separate from core.
        Used only for the external flaky-signal fetch, which is a one-off
        per repo, not a hot path.
        """
        yield from self.paginate("/search/issues", params={"q": query}, items_key="items")
