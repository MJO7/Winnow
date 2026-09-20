# Applied FIRST, on its own, before the main stack ever runs `apply`.
# A $10 budget with e-mail alerts at 50 / 80 / 100% of actual spend and
# at 100% forecast. Kept as a separate root module so it can't be torn
# down by a `destroy` of the main stack -- the alarm outlives the system.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.60" }
  }
}

provider "aws" {
  region = var.region
}

variable "region" { default = "us-east-1" }
variable "alert_email" { type = string }
variable "monthly_limit_usd" { default = "10" }

resource "aws_budgets_budget" "winnow" {
  name         = "winnow-monthly"
  budget_type  = "COST"
  limit_amount = var.monthly_limit_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
