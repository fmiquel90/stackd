# Smallest sensible deployment: two AZs, one small API task, one worker host.
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

  # Local state is fine to try it out. For anything you keep, use the production example's
  # backend (encrypted S3 bucket, versioned, with a lock file).
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = { Project = var.name, ManagedBy = "opentofu" }
  }
}

# CloudFront certificates live in us-east-1.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = { Project = var.name, ManagedBy = "opentofu" }
  }
}

module "stackd" {
  source = "../../"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  name               = var.name
  availability_zones = ["${var.aws_region}a", "${var.aws_region}b"]

  domain_name     = var.domain_name
  route53_zone_id = var.route53_zone_id

  google_client_id       = var.google_client_id
  google_allowed_domains = var.google_allowed_domains

  # Null on the bootstrap apply; then the digests of the images you pushed.
  api_image     = var.api_image
  worker_image  = var.worker_image
  runner_images = var.runner_images

  db_instance_class       = "db.t4g.micro"
  db_deletion_protection  = false
  alb_deletion_protection = false

  api_cpu           = 256
  api_memory        = 512
  api_desired_count = 1

  worker_count         = 1
  worker_instance_type = "t3.medium"
}

variable "name" {
  type    = string
  default = "stackd"
}

variable "aws_region" {
  type    = string
  default = "eu-west-1"
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
