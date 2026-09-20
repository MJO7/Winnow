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
