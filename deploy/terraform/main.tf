# Stackd on AWS. See docs/guide/deploying-to-aws.md.
#
#   browser ──HTTPS──▶ CloudFront ──┬─▶ S3 (SPA, Origin Access Control)
#                                   └─HTTPS (VPC origin)─▶ internal ALB ──HTTPS──▶ API (ECS Fargate)
#   worker hosts (EC2, docker runner) ──HTTPS──▶ internal ALB                       │
#                                                     RDS PostgreSQL 18 ◀───────────┤
#                                                     S3 artifacts (SSE-KMS) ◀──────┤
#                                                     KMS (OIDC signing key) ◀──────┘
#
# The providers are configured by the caller (examples/): this module only declares them.

module "network" {
  source = "./modules/network"

  name               = var.name
  vpc_cidr           = var.vpc_cidr
  availability_zones = var.availability_zones
}

module "kms" {
  source = "./modules/kms"
  name   = var.name
}

module "ecr" {
  source = "./modules/ecr"
  name   = var.name
}

module "storage" {
  source = "./modules/storage"

  name        = var.name
  kms_key_arn = module.kms.data_key_arn
}

module "secrets" {
  source = "./modules/secrets"

  name               = var.name
  kms_key_arn        = module.kms.data_key_arn
  jwt_secret_version = var.jwt_secret_version
}

module "database" {
  source = "./modules/database"

  name                  = var.name
  vpc_id                = module.network.vpc_id
  subnet_ids            = module.network.private_subnet_ids
  kms_key_arn           = module.kms.data_key_arn
  engine_version        = var.db_engine_version
  instance_class        = var.db_instance_class
  multi_az              = var.db_multi_az
  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  deletion_protection   = var.db_deletion_protection
  password_version      = var.db_password_version
}

# ── Certificates ─────────────────────────────────────────────────────────────────────────────
# Created in module.certificates, waited on here: `apply -target=module.<name>.module.certificates`
# creates them without blocking, for the bootstrap without Route 53 (see the guide).

module "certificates" {
  source = "./modules/certificates"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  domain_name          = var.domain_name
  internal_domain_name = local.internal_domain_name
  create_public        = local.create_public_certificate
  create_internal      = local.create_internal_certificate
  route53_zone_id      = var.route53_zone_id
}

resource "aws_acm_certificate_validation" "public" {
  count    = local.create_public_certificate ? 1 : 0
  provider = aws.us_east_1

  certificate_arn         = module.certificates.public_certificate_arn
  validation_record_fqdns = local.route53_validation ? module.certificates.public_validation_fqdns : null
}

resource "aws_acm_certificate_validation" "internal" {
  count = local.create_internal_certificate ? 1 : 0

  certificate_arn         = module.certificates.internal_certificate_arn
  validation_record_fqdns = local.route53_validation ? module.certificates.internal_validation_fqdns : null
}

# ── API ──────────────────────────────────────────────────────────────────────────────────────

module "iam" {
  source = "./modules/iam"

  name            = var.name
  api_secret_arns = [
    module.database.database_url_secret_arn,
    module.secrets.encryption_key_secret_arn,
    module.secrets.jwt_secret_secret_arn,
    module.secrets.google_client_secret_arn,
  ]
  data_key_arn         = module.kms.data_key_arn
  oidc_signing_key_arn = module.kms.oidc_signing_key_arn
  artifacts_bucket_arn = module.storage.artifacts_bucket_arn
}

module "api" {
  source = "./modules/api"

  name                    = var.name
  region                  = local.region
  enabled                 = local.services_enabled
  vpc_id                  = module.network.vpc_id
  subnet_ids              = module.network.private_subnet_ids
  certificate_arn         = local.internal_certificate_arn
  alb_deletion_protection = var.alb_deletion_protection
  execution_role_arn      = module.iam.api_execution_role_arn
  task_role_arn           = module.iam.api_task_role_arn
  image                   = var.api_image
  cpu                     = var.api_cpu
  memory                  = var.api_memory
  cpu_architecture        = var.api_cpu_architecture
  desired_count           = var.api_desired_count
  run_migrations          = var.run_migrations
  log_retention_days      = var.log_retention_days

  environment = {
    STACKD_ENV             = "production"
    STACKD_LOG_FORMAT      = "json"
    STACKD_PUBLIC_URL      = local.public_url
    STACKD_APP_URL         = local.public_url
    STACKD_INTERNAL_URL    = local.internal_url
    STACKD_ALLOWED_DOMAINS = join(",", var.google_allowed_domains)
    GOOGLE_CLIENT_ID       = var.google_client_id
    # Workload OIDC tokens signed by KMS (SPECS §3.11): required in production.
    STACKD_OIDC_SIGNER      = "kms"
    STACKD_OIDC_KMS_KEY_ARN = module.kms.oidc_signing_key_arn
    # No S3_ACCESS_KEY / S3_ENDPOINT_URL: the S3 client uses the task role.
    S3_BUCKET  = module.storage.artifacts_bucket_id
    AWS_REGION = local.region
    # Proxy headers are trusted from the private subnets only: the ALB nodes and CloudFront's VPC
    # origin ENIs live there; workers do not (their subnets are apart).
    FORWARDED_ALLOW_IPS = join(",", module.network.private_subnet_cidrs)
  }

  secrets = {
    DATABASE_URL          = module.database.database_url_secret_arn
    STACKD_ENCRYPTION_KEY = module.secrets.encryption_key_secret_arn
    STACKD_JWT_SECRET     = module.secrets.jwt_secret_secret_arn
    GOOGLE_CLIENT_SECRET  = module.secrets.google_client_secret_arn
  }
}

# RDS: the API tasks (and the migration task, same security group) only.
resource "aws_vpc_security_group_ingress_rule" "db_from_api" {
  security_group_id            = module.database.security_group_id
  referenced_security_group_id = module.api.api_security_group_id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "PostgreSQL from the API tasks"
}

# Internal ALB, HTTPS only: CloudFront's VPC origin ENIs (private subnets) and the workers. The
# CloudFront API does not return the security group of its VPC origin ENIs: subnets it is.
resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  for_each = toset(concat(module.network.private_subnet_cidrs, module.network.worker_subnet_cidrs))

  security_group_id = module.api.alb_security_group_id
  cidr_ipv4         = each.value
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  description       = "HTTPS from ${each.value}"
}

# internal_domain_name → internal ALB (private addresses in a public zone: resolvable by CloudFront
# and the workers, reachable from the VPC only).
resource "aws_route53_record" "internal" {
  count = var.route53_zone_id == null ? 0 : 1

  zone_id = var.route53_zone_id
  name    = local.internal_domain_name
  type    = "A"

  alias {
    name                   = module.api.alb_dns_name
    zone_id                = module.api.alb_zone_id
    evaluate_target_health = false
  }
}

# ── CloudFront + SPA ─────────────────────────────────────────────────────────────────────────

module "cdn" {
  source = "./modules/cdn"

  name                            = var.name
  domain_name                     = var.domain_name
  internal_domain_name            = local.internal_domain_name
  certificate_arn                 = local.public_certificate_arn
  route53_zone_id                 = var.route53_zone_id
  alb_arn                         = module.api.alb_arn
  spa_bucket_regional_domain_name = module.storage.spa_bucket_regional_domain_name
  spa_oac_id                      = module.storage.spa_oac_id
  price_class                     = var.cloudfront_price_class
}

data "aws_iam_policy_document" "spa_bucket" {
  statement {
    sid       = "CloudFrontRead"
    actions   = ["s3:GetObject"]
    resources = ["${module.storage.spa_bucket_arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [module.cdn.distribution_arn]
    }
  }
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [
      module.storage.spa_bucket_arn,
      "${module.storage.spa_bucket_arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "spa" {
  bucket = module.storage.spa_bucket_id
  policy = data.aws_iam_policy_document.spa_bucket.json
}

# ── Workers ──────────────────────────────────────────────────────────────────────────────────

module "worker" {
  source = "./modules/worker"
  count  = local.workers_enabled ? 1 : 0

  name                  = var.name
  region                = local.region
  vpc_id                = module.network.vpc_id
  subnet_ids            = module.network.worker_subnet_ids
  api_url               = local.internal_url
  worker_image          = var.worker_image
  runner_images         = var.runner_images
  ecr_repository_arns   = [module.ecr.repository_arns["worker"], module.ecr.repository_arns["runner"]]
  pool_token_secret_arn = module.secrets.worker_pool_token_secret_arn
  data_key_arn          = module.kms.data_key_arn
  worker_count          = var.worker_count
  instance_type         = var.worker_instance_type
  architecture          = var.worker_architecture
  ami_id                = var.worker_ami_id
  root_volume_size      = var.worker_root_volume_size
  max_concurrent_jobs   = var.worker_max_concurrent_jobs
  shutdown_timeout      = var.worker_shutdown_timeout_seconds
  docker_memory         = var.worker_docker_memory
  docker_cpus           = var.worker_docker_cpus
  enable_ssm            = var.worker_enable_ssm
  log_retention_days    = var.log_retention_days
}
