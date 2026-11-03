# RDS PostgreSQL 18 (uuidv7(), PLAN §6) and the API's DATABASE_URL secret.
#
# No password ever reaches the state or the plan: a random one is generated as an ephemeral value
# and written once, write-only, to Secrets Manager (`secret_string_wo`). RDS and the DATABASE_URL
# secret both read it back from there (ephemeral read, write-only arguments), so they can never
# disagree. Bump `password_version` to rotate. RDS-managed master passwords
# (`manage_master_user_password`) are not used: they rotate every 7 days by default and the API
# reads DATABASE_URL once, at startup.

locals {
  family = "postgres${split(".", var.engine_version)[0]}"
}

ephemeral "random_password" "db" {
  length  = 40
  special = false # also keeps the URL free of characters to escape
}

resource "aws_secretsmanager_secret" "password" {
  name                    = "${var.name}/database-password"
  description             = "Master password of the ${var.name} RDS instance"
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "password" {
  secret_id                = aws_secretsmanager_secret.password.id
  secret_string_wo         = ephemeral.random_password.db.result
  secret_string_wo_version = var.password_version
}

# The password as stored — what RDS and the URL get, whatever apply wrote it.
ephemeral "aws_secretsmanager_secret_version" "password" {
  secret_id  = aws_secretsmanager_secret.password.id
  depends_on = [aws_secretsmanager_secret_version.password]
}

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
}

resource "aws_db_parameter_group" "this" {
  name_prefix = "${var.name}-${local.family}-"
  family      = local.family

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_connections"
    value = "1"
  }

  parameter {
    name  = "log_disconnections"
    value = "1"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "Stackd RDS: ingress from the API tasks only (rules in the root module)"
  vpc_id      = var.vpc_id
}

resource "aws_db_instance" "this" {
  identifier = var.name

  engine                     = "postgres"
  engine_version             = var.engine_version
  auto_minor_version_upgrade = true
  instance_class             = var.instance_class

  db_name             = "stackd"
  username            = "stackd"
  password_wo         = ephemeral.aws_secretsmanager_secret_version.password.secret_string
  password_wo_version = var.password_version

  db_subnet_group_name   = aws_db_subnet_group.this.name
  parameter_group_name   = aws_db_parameter_group.this.name
  vpc_security_group_ids = [aws_security_group.db.id]

  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage > 0 ? var.max_allocated_storage : null
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = var.kms_key_arn

  multi_az                  = var.multi_az
  publicly_accessible       = false
  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = !var.deletion_protection
  final_snapshot_identifier = var.deletion_protection ? "${var.name}-final" : null
  copy_tags_to_snapshot     = true

  backup_retention_period         = 7
  backup_window                   = "03:00-04:00"
  maintenance_window              = "Mon:04:00-Mon:05:00"
  enabled_cloudwatch_logs_exports = ["postgresql"]
}

# DATABASE_URL, as the API reads it (asyncpg driver, TLS required).
resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.name}/database-url"
  description             = "DATABASE_URL of the ${var.name} API"
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id
  secret_string_wo = format(
    "postgresql+asyncpg://stackd:%s@%s:%d/stackd?ssl=require",
    ephemeral.aws_secretsmanager_secret_version.password.secret_string,
    aws_db_instance.this.address,
    aws_db_instance.this.port,
  )
  # Re-written when the password rotates (the address is stable: the identifier is fixed).
  secret_string_wo_version = var.password_version
}
