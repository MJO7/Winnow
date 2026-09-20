# RDS Postgres, publicly reachable, TLS forced. This is topology option 3
# from the spec and docs/adr/0003: Lambdas that need both Postgres and
# the public internet (GitHub, the model API) would otherwise force a NAT
# gateway at ~$32/month -- more than every other component combined.
#
# Exposure accepted, and stated: the endpoint is reachable from the
# internet on 5432. Mitigations: rds.force_ssl=1, a 32-char random
# password held only in Secrets Manager, no sensitive data in the
# database (it holds public CI history), and the security group below.
# With a production budget: private subnets + NAT (or split placement
# with an S3 gateway endpoint), and RDS Proxy.

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "db" {
  name                    = "${local.name}/db"
  recovery_window_in_days = 0 # destroy must be immediate, not a 7-day soft delete
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    username = "winnow"
    password = random_password.db.result
    host     = aws_db_instance.pg.address
    port     = aws_db_instance.pg.port
    dbname   = "winnow"
    url      = "postgresql://winnow:${random_password.db.result}@${aws_db_instance.pg.address}:${aws_db_instance.pg.port}/winnow?sslmode=require"
  })
}

resource "aws_security_group" "rds" {
  name        = "${local.name}-rds"
  description = "Postgres: admin workstation + Lambda egress (public ranges)"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "psql from the admin workstation"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }

  # Lambdas outside a VPC egress from AWS's public ranges, which are too
  # numerous to enumerate in a security group. This rule is the stated
  # exposure. TLS is forced by the parameter group; auth is the secret.
  ingress {
    description = "Lambda egress (public AWS ranges) - see docs/adr/0003"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_subnet_group" "pg" {
  name       = "${local.name}-pg"
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_db_parameter_group" "pg" {
  name   = "${local.name}-pg16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
}

resource "aws_db_instance" "pg" {
  identifier        = "${local.name}-pg"
  engine            = "postgres"
  engine_version    = "16"
  instance_class    = var.rds_instance_class
  allocated_storage = 20
  storage_type      = "gp3"

  db_name  = "winnow"
  username = "winnow"
  password = random_password.db.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.pg.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.pg.name
  publicly_accessible    = true
  multi_az               = false

  backup_retention_period = 0 # reproducible from S3; backups are a cost line
  skip_final_snapshot     = true
  deletion_protection     = false
  apply_immediately       = true

  performance_insights_enabled = false
}
