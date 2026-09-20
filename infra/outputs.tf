output "api_url" {
  value = aws_lambda_function_url.api.function_url
}

output "rds_endpoint" {
  value = aws_db_instance.pg.address
}

output "db_secret_arn" {
  value = aws_secretsmanager_secret.db.arn
}

output "log_bucket" {
  value = aws_s3_bucket.logs.bucket
}

output "job_queue_url" {
  value = aws_sqs_queue.jobs.url
}

output "dlq_url" {
  value = aws_sqs_queue.jobs_dlq.url
}

output "signature_table" {
  value = aws_dynamodb_table.signatures.name
}

output "post_apply" {
  value = <<-EOT
    1. aws secretsmanager put-secret-value --secret-id winnow/github-token  --secret-string "$GITHUB_TOKEN"
    2. aws secretsmanager put-secret-value --secret-id winnow/anthropic-key --secret-string "$ANTHROPIC_API_KEY"
    3. aws lambda invoke --function-name winnow-label --payload '{"action":"migrate"}' /dev/stdout
    4. aws lambda invoke --function-name winnow-discover --payload '{"days":1,"per_day":5}' /dev/stdout
    5. confirm the SNS e-mail subscription, or no alarm will ever reach you
  EOT
}
