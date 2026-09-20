# Runbook

## SLO

- **Triage latency:** 95% of `/triage` requests complete within 2 s (Lambda `Duration` p95 on `winnow-api`). Retrieval is ~10 ms; the model call is the budget. The threshold is the Phase 4 generation p95 for the deployed configuration plus headroom, not a round number.
- **Ingest lag:** the newest run seen by `discover` is under 15 minutes old (`Winnow/IngestLagSeconds`). One missed hourly schedule plus a rate-limit sleep is ~10 minutes; past 15 something is stuck, not slow.

## Alarms

| Alarm | Means | First three checks |
|---|---|---|
| `winnow-dlq-not-empty` | A run failed `fetch` three times. Work is being dropped. | 1. `aws sqs receive-message --queue-url <dlq_url> --max-number-of-messages 5` and read the run_ids. 2. CloudWatch Logs Insights on `/aws/lambda/winnow-fetch`: `fields @timestamp, run_id, error \| filter ispresent(error)`. 3. Is it one run (poison: a malformed log, a job with 10k steps) or every run (token expired, RDS down, GitHub 5xx)? |
| `winnow-api-p95-over-2s` | Triage is slow. | 1. Split retrieval vs generation from the `latency_ms` field in responses / logs. 2. If generation: model API status, or the prompt grew (k too high, long messages). 3. If retrieval: `EXPLAIN ANALYZE` the tsvector query; check `failure_signatures` row count vs when the index was last analyzed. Cold starts show as a p95 step, not a drift. |
| `winnow-ingest-lag-over-15m` | `discover` isn't seeing new runs. | 1. Is the schedule rule enabled (`discover_schedule_enabled`)? 2. `/aws/lambda/winnow-discover` last invocation: rate-limit sleep (`Rate limit low`) or GitHub returning stale listings (see ADR 0001 — use `created=` windows, never `event=` ordering). 3. Secrets: has the GitHub token expired (401s)? |

Confirm the SNS e-mail subscription after apply or none of these reach anyone.

## Procedures

**Drain the DLQ after fixing the cause.** Redrive to the source queue (the redrive-allow policy permits it):
```
aws sqs start-message-move-task --source-arn <dlq_arn> --destination-arn <jobs_arn>
```
If the messages are poison (same run fails deterministically), fix `fetch` first; redriving without a fix just triples the noise.

**Replay from S3.** Raw logs are the source of truth. To rebuild signatures after a normalizer change:
```
psql "$DB_URL" -c "DELETE FROM failure_signatures WHERE job_id IN (...)"
```
then re-enqueue the affected run_ids to the jobs queue:
```
aws sqs send-message --queue-url <jobs_url> --message-body '{"owner":"pytorch","repo":"pytorch","repo_id":1,"run_id":123}'
```
`fetch` sees a `fetched` job with no signature row and reads the gzipped log back from S3 instead of GitHub (`lambdas/fetch.py`, `stats.replayed`). This works after GitHub's retention has expired; that is what the archive is for. DynamoDB items are overwritten by `put_item`.

**Roll back a bad signature normalizer.** `git revert` the commit, rebuild zips (`infra/build_lambdas.sh`), `terraform apply` (only the four function hashes change), then replay as above. Labels are unaffected — they derive from run/job metadata, not signatures.

**Rotate the DB password.** `terraform taint random_password.db && terraform apply`. Lambdas pick the new secret up on the next cold start; force one with `aws lambda update-function-configuration --function-name winnow-api --description "rotated $(date +%s)"`.

**Stop the bill.** `terraform destroy` in `infra/`. Verify with `aws ce get-cost-and-usage` the following day. The budget module in `infra/budget` is left in place on purpose.

## Recorded incident

_To be written after the first injected failure in Phase 7: timeline (detection, first useful signal, diagnosis, fix, verification) and what would have made detection faster._
