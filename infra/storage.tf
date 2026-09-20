# ---- S3: raw logs, gzipped, the source of truth for reprocessing -------

resource "aws_s3_bucket" "logs" {
  bucket        = "${local.name}-raw-logs-${local.account_id}"
  force_destroy = true # `terraform destroy` must leave nothing billable
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    id     = "ia-after-30d"
    status = "Enabled"
    filter {}
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    # Hard cap on retained volume: nothing older than the labeling window
    # is ever re-read, and IA charges a 30-day minimum anyway.
    expiration {
      days = 120
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ---- SQS: backpressure between discover and fetch ----------------------

resource "aws_sqs_queue" "jobs_dlq" {
  name                      = "${local.name}-jobs-dlq"
  message_retention_seconds = 14 * 24 * 3600
}

resource "aws_sqs_queue" "jobs" {
  name = "${local.name}-jobs"
  # Must exceed the fetch Lambda's timeout, or a slow log download gets
  # redelivered mid-flight and processed twice.
  visibility_timeout_seconds = 6 * 60
  message_retention_seconds  = 4 * 24 * 3600
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "jobs_dlq" {
  queue_url = aws_sqs_queue.jobs_dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.jobs.arn]
  })
}

# ---- DynamoDB: signature + job metadata, point lookup by job_id ---------
#
# Provisioned at the always-free 25/25 allocation rather than on-demand:
# the 25 GB free tier covers storage only, and on-demand writes bill from
# the first request. At this ingest rate 25 WCU is far more than needed.

resource "aws_dynamodb_table" "signatures" {
  name           = "${local.name}-signatures"
  billing_mode   = "PROVISIONED"
  read_capacity  = 25
  write_capacity = 25
  hash_key       = "job_id"

  attribute {
    name = "job_id"
    type = "N"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false # costs money; the data is reproducible from S3
  }
}
