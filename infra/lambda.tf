# Four functions, one zip each, built by infra/build_lambdas.sh into
# build/. The zip vendors src/winnow plus arm64 wheels; no torch -- the
# API retrieves with tsvector (docs/adr/0004: it matched pgvector within
# noise on this corpus and keeps the package under 50 MB).

resource "aws_secretsmanager_secret" "github" {
  name                    = "${local.name}/github-token"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret" "anthropic" {
  name                    = "${local.name}/anthropic-key"
  recovery_window_in_days = 0
}
# Values are set after apply, never through Terraform (they'd land in state):
#   aws secretsmanager put-secret-value --secret-id winnow/github-token --secret-string "$GITHUB_TOKEN"
#   aws secretsmanager put-secret-value --secret-id winnow/anthropic-key --secret-string "$ANTHROPIC_API_KEY"

locals {
  common_env = {
    WINNOW_DB_SECRET_ARN        = aws_secretsmanager_secret.db.arn
    WINNOW_GITHUB_SECRET_ARN    = aws_secretsmanager_secret.github.arn
    WINNOW_ANTHROPIC_SECRET_ARN = aws_secretsmanager_secret.anthropic.arn
    WINNOW_LOG_BUCKET           = aws_s3_bucket.logs.bucket
    WINNOW_SIGNATURE_TABLE      = aws_dynamodb_table.signatures.name
    WINNOW_JOB_QUEUE_URL        = aws_sqs_queue.jobs.url
    WINNOW_REPOS                = join(",", var.repos)
    WINNOW_TRIAGE_MODEL         = var.triage_model
    WINNOW_TRIAGE_K             = tostring(var.triage_k)
  }

  functions = {
    discover = { handler = "discover.handler", timeout = 300, memory = 256 }
    fetch    = { handler = "fetch.handler", timeout = 300, memory = 512 }
    label    = { handler = "label.handler", timeout = 900, memory = 512 }
    api      = { handler = "api.handler", timeout = 30, memory = 1024 }
  }
}

resource "aws_cloudwatch_log_group" "fn" {
  for_each          = local.functions
  name              = "/aws/lambda/${local.name}-${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "fn" {
  for_each = local.functions

  function_name    = "${local.name}-${each.key}"
  role             = aws_iam_role.fn[each.key].arn
  handler          = each.value.handler
  runtime          = "python3.12"
  architectures    = [var.lambda_architecture]
  timeout          = each.value.timeout
  memory_size      = each.value.memory
  filename         = "${path.module}/../build/lambda_${each.key}.zip"
  source_code_hash = filebase64sha256("${path.module}/../build/lambda_${each.key}.zip")

  environment {
    variables = local.common_env
  }

  depends_on = [aws_cloudwatch_log_group.fn]
}

# discover -> SQS -> fetch
resource "aws_lambda_event_source_mapping" "fetch_from_queue" {
  event_source_arn        = aws_sqs_queue.jobs.arn
  function_name           = aws_lambda_function.fn["fetch"].arn
  batch_size              = 1 # one run per invocation: a run's log downloads can take minutes
  function_response_types = ["ReportBatchItemFailures"]
  scaling_config {
    # GitHub's 5,000 req/hr is the real ceiling; a wide fan-out just burns
    # it faster and then everything sleeps. Two workers saturate it.
    maximum_concurrency = 2
  }
}

# Hourly discover. A schedule rule is the smallest thing that can invoke
# a Lambda on a timer; nothing else from EventBridge is used.
resource "aws_cloudwatch_event_rule" "discover_hourly" {
  name                = "${local.name}-discover-hourly"
  schedule_expression = "rate(1 hour)"
  state               = var.discover_schedule_enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "discover" {
  rule = aws_cloudwatch_event_rule.discover_hourly.name
  arn  = aws_lambda_function.fn["discover"].arn
}

resource "aws_lambda_permission" "discover_from_events" {
  statement_id  = "AllowEventsInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["discover"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.discover_hourly.arn
}

# The triage endpoint. A Function URL with no auth is the zero-surface
# choice over API Gateway (ADR: Fargate cut, API Gateway not needed).
# AWS_IAM auth would be free too; NONE is chosen so the endpoint can be
# curl'd in the README without SigV4 -- it exposes public CI history.
resource "aws_lambda_function_url" "api" {
  function_name      = aws_lambda_function.fn["api"].function_name
  authorization_type = "NONE"
}
