# Production deployment: three AZs, Multi-AZ RDS, two API tasks, a worker fleet.
# Bootstrap and day-2 steps: docs/guide/deploying-to-aws.md.

terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.50"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }

  # Create the bucket beforehand: versioning on, public access blocked, default encryption.
  # The state holds no secret value (write-only arguments), but it maps the whole deployment.
  backend "s3" {
    bucket       = "REPLACE_ME-tfstate"
    key          = "stackd/production/terraform.tfstate"
    region       = "REPLACE_ME"
    encrypt      = true
    use_lockfile = true # S3-native locking (OpenTofu >= 1.10, Terraform >= 1.10)
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = { Project = var.name, Environment = "production", Owner = var.owner, ManagedBy = "opentofu" }
  }
}

# CloudFront certificates live in us-east-1.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = { Project = var.name, Environment = "production", Owner = var.owner, ManagedBy = "opentofu" }
  }
}

module "stackd" {
  source = "../../"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  name = var.name
  availability_zones = [
    "${var.aws_region}a",
    "${var.aws_region}b",
    "${var.aws_region}c",
  ]

  domain_name     = var.domain_name
  route53_zone_id = var.route53_zone_id

  google_client_id       = var.google_client_id
  google_allowed_domains = var.google_allowed_domains

  api_image     = var.api_image
  worker_image  = var.worker_image
  runner_images = var.runner_images

  db_instance_class        = "db.r8g.large"
  db_multi_az              = true
  db_allocated_storage     = 50
  db_max_allocated_storage = 500

  api_cpu           = 1024
  api_memory        = 2048
  api_desired_count = 2

  worker_count                    = 3
  worker_instance_type            = "m7i.large"
  worker_max_concurrent_jobs      = 2
  worker_shutdown_timeout_seconds = 1800
  worker_docker_memory            = "3g"
}

variable "name" {
  type    = string
  default = "stackd"
}

variable "aws_region" {
  type = string
}

variable "owner" {
  type = string
}

variable "domain_name" {
  type = string
}

variable "route53_zone_id" {
  type    = string
  default = null
}

variable "google_client_id" {
  type = string
}

variable "google_allowed_domains" {
  type = list(string)
}

variable "api_image" {
  type    = string
  default = null
}

variable "worker_image" {
  type    = string
  default = null
}

variable "runner_images" {
  type    = map(string)
  default = {}
}

output "public_url" {
  value = module.stackd.public_url
}

output "google_oauth_redirect_uri" {
  value = module.stackd.google_oauth_redirect_uri
}

output "acm_validation_records" {
  value = module.stackd.acm_validation_records
}

output "dns_records" {
  value = module.stackd.dns_records
}

output "ecr_repository_urls" {
  value = module.stackd.ecr_repository_urls
}

output "google_client_secret_arn" {
  value = module.stackd.google_client_secret_arn
}

output "worker_pool_token_secret_arn" {
  value = module.stackd.worker_pool_token_secret_arn
}

output "spa_bucket" {
  value = module.stackd.spa_bucket
}

output "cloudfront_distribution_id" {
  value = module.stackd.cloudfront_distribution_id
}

output "migrate_command" {
  value = module.stackd.migrate_command
}

output "ecs_cluster_name" {
  value = module.stackd.ecs_cluster_name
}

output "worker_autoscaling_group" {
  value = module.stackd.worker_autoscaling_group
}
