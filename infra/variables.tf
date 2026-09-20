variable "region" {
  default = "us-east-1"
}

variable "alert_email" {
  description = "Receives CloudWatch alarm notifications."
  type        = string
}

variable "admin_cidr" {
  description = "Your workstation's public IP as a /32, for psql access to RDS. Lambdas reach RDS from AWS's public ranges; see docs/adr/0003."
  type        = string
}

variable "repos" {
  description = "owner/name repos the discover Lambda samples. Must match winnow.repos.CORPUS."
  type        = list(string)
  default     = ["pytorch/pytorch", "home-assistant/core", "apache/airflow", "python/cpython", "pandas-dev/pandas"]
}

variable "discover_schedule_enabled" {
  description = "Whether the hourly discover schedule is on. Off by default: ingest is bursty and kicked manually while developing."
  default     = false
}

variable "rds_instance_class" {
  default = "db.t4g.micro"
}

variable "lambda_architecture" {
  # Graviton: ~20% cheaper per ms and the psycopg/pgvector wheels exist for aarch64.
  default = "arm64"
}

variable "log_retention_days" {
  default = 14
}

variable "triage_model" {
  description = "Model the API triages with. Set from the Phase 4 frontier (docs/adr/0005): the cheapest configuration at the target accuracy."
  default     = "claude-opus-5"
}

variable "triage_k" {
  default = 5
}
