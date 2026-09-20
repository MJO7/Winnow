# The SLO (docs/RUNBOOK.md): 95% of triage requests complete within 2 s,
# and ingest lag stays under 15 minutes. One alarm per SLO clause, plus
# the DLQ, which is the signal that the ingest path is dropping work.

resource "aws_sns_topic" "alarms" {
  name = "${local.name}-alarms"
}

resource "aws_sns_topic_subscription" "alarms_email" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# Anything in the DLQ means three attempts at a run failed. Threshold 0:
# there is no acceptable background rate of dropped runs.
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${local.name}-dlq-not-empty"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.jobs_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

# p95 of the API function's own duration. 2 s, not a round number picked
# by feel: retrieval is ~10 ms, the model call is the budget, and the
# eval's generation p95 sets the floor this can realistically hold.
resource "aws_cloudwatch_metric_alarm" "api_p95_latency" {
  alarm_name          = "${local.name}-api-p95-over-2s"
  namespace           = "AWS/Lambda"
  metric_name         = "Duration"
  dimensions          = { FunctionName = aws_lambda_function.fn["api"].function_name }
  extended_statistic  = "p95"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 2000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

# Ingest lag: emitted by discover as (now - newest run created_at seen).
# 15 min = one missed hourly schedule plus a rate-limit sleep; beyond
# that something is stuck, not slow.
resource "aws_cloudwatch_metric_alarm" "ingest_lag" {
  alarm_name          = "${local.name}-ingest-lag-over-15m"
  namespace           = "Winnow"
  metric_name         = "IngestLagSeconds"
  statistic           = "Maximum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 900
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}
