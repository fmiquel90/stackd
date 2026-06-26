# Dependency order (no cycles):
#   kms → database, storage, secrets(via kms_key_arn)
#   database → secrets(database_url)
#   secrets, ecr, network → iam, api, worker
#   api → cdn, worker

module "kms" {
  source = "./modules/kms"
  name   = var.name
}

module "network" {
  source = "./modules/network"

  name               = var.name
  vpc_cidr           = var.vpc_cidr
  availability_zones = var.availability_zones
}

module "ecr" {
  source = "./modules/ecr"
  name   = var.name
}

module "database" {
  source = "./modules/database"

  name       = var.name
  vpc_id     = module.network.vpc_id
  subnet_ids = module.network.private_subnet_ids
  kms_key_arn = module.kms.key_arn

  allowed_security_group_ids = [module.api.api_sg_id]

  instance_class        = var.db_instance_class
  multi_az              = var.db_multi_az
  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
}

module "secrets" {
  source = "./modules/secrets"

  name                 = var.name
  kms_key_arn          = module.kms.key_arn
  google_client_id     = var.google_client_id
  google_client_secret = var.google_client_secret
  worker_pool_token    = var.worker_pool_token
  database_url         = module.database.database_url
}

module "storage" {
  source = "./modules/storage"

  name        = var.name
  kms_key_arn = module.kms.key_arn
}

module "iam" {
  source = "./modules/iam"

  name                      = var.name
  aws_region                = var.aws_region
  artifacts_bucket_arn      = module.storage.artifacts_bucket_arn
  ecr_api_repository_arn    = module.ecr.api_repository_arn
  ecr_worker_repository_arn = module.ecr.worker_repository_arn
  secret_arns               = module.secrets.all_secret_arns
  kms_key_arn               = module.kms.key_arn
}

module "api" {
  source = "./modules/api"

  name       = var.name
  vpc_id     = module.network.vpc_id
  subnet_ids = module.network.private_subnet_ids
  aws_region = var.aws_region

  execution_role_arn = module.iam.execution_role_arn
  task_role_arn      = module.iam.api_task_role_arn

  image         = local.api_image
  cpu           = var.api_cpu
  memory        = var.api_memory
  desired_count = var.api_desired_count

  environment_variables = {
    STACKD_ENV             = "production"
    STACKD_LOG_FORMAT      = "json"
    STACKD_ALLOWED_DOMAINS = var.google_allowed_domains
    STACKD_PUBLIC_URL      = local.public_url
    STACKD_APP_URL         = local.public_url
    STACKD_INTERNAL_URL    = local.internal_url
    S3_BUCKET              = module.storage.artifacts_bucket_id
    AWS_REGION             = var.aws_region
    STACKD_RUN_SCHEDULER   = "true"
    STACKD_DEV_AUTH        = "false"
  }

  secrets = merge(
    {
      DATABASE_URL          = module.secrets.database_url_secret_arn
      STACKD_ENCRYPTION_KEY = module.secrets.encryption_key_secret_arn
      STACKD_JWT_SECRET     = module.secrets.jwt_secret_secret_arn
    },
    var.google_client_id != "" ? {
      GOOGLE_CLIENT_ID     = "${module.secrets.google_credentials_secret_arn}:client_id::"
      GOOGLE_CLIENT_SECRET = "${module.secrets.google_credentials_secret_arn}:client_secret::"
    } : {}
  )
}

module "cdn" {
  source = "./modules/cdn"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  name                            = var.name
  alb_arn                         = module.api.alb_arn
  alb_dns_name                    = module.api.alb_dns_name
  spa_bucket_regional_domain_name = module.storage.spa_bucket_regional_domain_name
  spa_oac_id                      = module.storage.spa_oac_id

  domain_name     = var.domain_name
  route53_zone_id = var.route53_zone_id
  certificate_arn = var.certificate_arn
}

module "worker" {
  source = "./modules/worker"

  name       = var.name
  vpc_id     = module.network.vpc_id
  subnet_ids = module.network.private_subnet_ids
  aws_region = var.aws_region

  cluster_arn        = module.api.cluster_arn
  execution_role_arn = module.iam.execution_role_arn
  task_role_arn      = module.iam.worker_task_role_arn

  image         = local.worker_image
  cpu           = var.worker_cpu
  memory        = var.worker_memory
  desired_count = var.worker_desired_count

  environment_variables = {
    STACKD_API_URL = local.internal_url
    STACKD_RUNNER  = "docker"
    AWS_REGION     = var.aws_region
  }

  secrets = {
    STACKD_POOL_TOKEN = module.secrets.worker_pool_token_secret_arn
  }
}

# ── Cross-module wiring (avoid putting these inside modules to prevent cycles) ─

# ALB ingress from private subnets — covers both CloudFront VPC Origin ENIs and worker tasks.
# Workers connect via this ALB; CloudFront VPC Origin creates ENIs in these same subnets.
resource "aws_vpc_security_group_ingress_rule" "alb_from_private_subnets" {
  for_each = toset(module.network.private_subnet_cidrs)

  security_group_id = module.api.alb_sg_id
  cidr_ipv4         = each.value
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  description       = "Private subnet ${each.value} (CloudFront VPC Origin + workers)"
}

# SPA S3 bucket policy: allow reads from this specific CloudFront distribution only
resource "aws_s3_bucket_policy" "spa" {
  bucket = module.storage.spa_bucket_id
  policy = data.aws_iam_policy_document.spa_bucket.json
}

data "aws_iam_policy_document" "spa_bucket" {
  statement {
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    actions   = ["s3:GetObject"]
    resources = ["${module.storage.spa_bucket_arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [module.cdn.cloudfront_distribution_arn]
    }
  }
}
