# Winnow on AWS. Every resource here earns its place; see docs/adr/0003
# for the network topology decision and README for the service table.
#
# Apply order: infra/budget first (the $10 alarm), then this.
# `terraform destroy` here must return the account to zero cost; the
# only things that persist are the budget module and CloudWatch log
# groups' already-ingested bytes (billed once, at ingest).

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.60" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
    archive = { source = "hashicorp/archive", version = "~> 2.5" }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "winnow", managed_by = "terraform" }
  }
}

data "aws_caller_identity" "me" {}
data "aws_region" "current" {}

locals {
  name       = "winnow"
  account_id = data.aws_caller_identity.me.account_id
  region     = data.aws_region.current.name
}
