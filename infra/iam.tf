# One role per function. Identity policies (attached to the role) grant
# the function's own permissions; resource policies (on SQS/S3/etc.)
# aren't needed here because every caller is in this account and acts
# through its own identity. The only Resource: "*" entries are for API
# actions that AWS doesn't scope to a resource ARN (PutMetricData).

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "fn" {
  for_each           = local.functions
  name               = "${local.name}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# Shared by all four: write own log stream, read own secrets, emit metrics.
data "aws_iam_policy_document" "fn_base" {
  for_each = local.functions

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.fn[each.key].arn}:*"]
  }

  statement {
    sid       = "ReadDbSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db.arn]
  }

  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"] # PutMetricData is not resource-scoped
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["Winnow"]
    }
  }
}

resource "aws_iam_role_policy" "fn_base" {
  for_each = local.functions
  role     = aws_iam_role.fn[each.key].id
  name     = "base"
  policy   = data.aws_iam_policy_document.fn_base[each.key].json
}

# discover: GitHub token + send to queue
data "aws_iam_policy_document" "discover" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.github.arn]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.jobs.arn]
  }
}

resource "aws_iam_role_policy" "discover" {
  role   = aws_iam_role.fn["discover"].id
  name   = "discover"
  policy = data.aws_iam_policy_document.discover.json
}

# fetch: consume queue, GitHub token, write logs to S3, write signatures to Dynamo
data "aws_iam_policy_document" "fetch" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.jobs.arn]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.github.arn]
  }
  statement {
    actions   = ["s3:PutObject", "s3:GetObject"] # GetObject: replay from the archive
    resources = ["${aws_s3_bucket.logs.arn}/logs/*"]
  }
  statement {
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.signatures.arn]
  }
}

resource "aws_iam_role_policy" "fetch" {
  role   = aws_iam_role.fn["fetch"].id
  name   = "fetch"
  policy = data.aws_iam_policy_document.fetch.json
}

# label: GitHub token for compare-commits; nothing else beyond Postgres
data "aws_iam_policy_document" "label" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.github.arn]
  }
}

resource "aws_iam_role_policy" "label" {
  role   = aws_iam_role.fn["label"].id
  name   = "label"
  policy = data.aws_iam_policy_document.label.json
}

# api: model key, point-read Dynamo (the hot path), read raw log from S3 on demand
data "aws_iam_policy_document" "api" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.anthropic.arn]
  }
  statement {
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.signatures.arn]
  }
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.logs.arn}/logs/*"]
  }
}

resource "aws_iam_role_policy" "api" {
  role   = aws_iam_role.fn["api"].id
  name   = "api"
  policy = data.aws_iam_policy_document.api.json
}
