# AI tools in this project

Kept as a factual log, updated per phase. The point of this file is the last section.

## Agent-assisted

- Project scaffolding: `pyproject.toml`, `docker-compose.yml`, `.gitignore`, the migration runner.
- The GitHub REST client's boilerplate: headers, Link-header pagination, `responses`-based test scaffolding.
- Test fixtures for the labeler (`tests/conftest.py` insert helpers).
- First drafts of the Markdown report renderer and this documentation.

## Reasoned about line by line, agent or not

- `normalize.py`. The eval is only as good as the signature; every regex substitution was chosen against a concrete failure shape and pinned by a test.
- The labeling rules in `label.py` and the exclusion registry. Each rule maps to a spec'd contaminating case and has a test for the case it catches and the case it must not catch.
- The auto-retry heuristic thresholds.

## An agent produced something plausible and wrong (Phase 0)

**What it generated.** In `github_client.py`, a rate-limit throttle:

```python
LOW_WATERMARK = 50
...
if rl.remaining <= LOW_WATERMARK:
    time.sleep(rl.seconds_until_reset() + 1.0)
```

**Why it looked right.** Against the authenticated 5,000 req/hr limit, reserving 50 requests is a sensible 1% headroom. It reads like textbook backpressure and the unit tests for retry/backoff all passed.

**What exposed it.** The first live smoke test ran unauthenticated (60 req/hr). The client made ten requests, saw `remaining: 50`, and slept for **3,165 seconds**. The log line was `Rate limit low (50/60 remaining) -- sleeping 3165s until reset`. Nothing crashed; it just stopped.

**The fix.** The watermark is now a fraction of the observed limit with a floor: `max(3, int(limit * 0.02))`. A regression test (`test_throttle_watermark_scales_with_limit`) pins 40/60 → no throttle, 2/60 → throttle, 80/5000 → throttle.

**Second instance, same session.** The generated test fixture truncated `DATABASE_URL` directly. Running the suite wiped the smoke-ingest data from the dev database and left synthetic `acme/widgets` rows behind. Fixed by giving tests their own `winnow_test` database, created and migrated on first use.

Both were caught by running the thing, not by reading it. That is the argument for the smoke test being part of the phase exit criterion rather than an afterthought.


## Plausible and wrong, Phases 1–3

**The pytest summary regex required a message.** `^(FAILED|ERROR)\s+(\S+)\s+-\s+(.*)$` matches every example in the pytest docs. It matches none of pandas' 104 `FAILED path::test[param]` lines, which pytest emits without the ` - message` suffix under some `-r` flags. The normalizer silently fell through to the traceback and tail extractors, and the first corpus report showed 52% of signatures on the tail fallback. Caught by grepping the actual logs for `FAILED` line shapes; fixed by making the suffix optional and taking the first `E   ` assertion line as the message.

**"Most recent 500 runs" assumed the listing is ordered.** `discover_runs` walked `GET /actions/runs?event=push` and stopped after 500. For pytorch the API returned a 7-hour slice from a month earlier as "newest" — reproducible: with `event=push` the first page is 2026-08-26; without the filter, or with `created>=2026-09-18`, it is today. Nothing in the client, the tests, or the log line `discovered 1000 run records` hinted at it. Caught only because the labeling pass produced zero flakes and the per-repo date range was printed while diagnosing that. Fixed with per-day `created=` windows.

**Restricting the flake rule to push/default was the spec's assumption, and the data disagreed.** Zero flakes in 84k jobs; 11 of the 12 fail→pass re-runs were on PR runs. The fix was not to loosen the rule but to check GitHub's documentation for what a re-run actually checks out: the original `GITHUB_SHA`. So same-run attempts are SHA-stable on any event, and only cross-run resolution needs the restriction. This one is in ADR 0001.
