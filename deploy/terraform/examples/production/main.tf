terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.77"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6"
    }
  }

  backend "s3" {
    # Fill in before first apply
    bucket = "REPLACE_ME_tfstate-bucket"
    key    = "stackd/production/terraform.tfstate"
    region = "REPLACE_ME_aws_region"
  }
}

provider "aws" {
  region = var.aws_region
}

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

module "stackd" {
  source = "../../"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  name       = var.name
  aws_region = var.aws_region

  availability_zones = [
    "${var.aws_region}a",
    "${var.aws_region}b",
    "${var.aws_region}c",
  ]

  db_instance_class        = "db.r8g.large"
  db_multi_az              = true
  db_allocated_storage     = 50
  db_max_allocated_storage = 500

  api_cpu           = 1024
  api_memory        = 2048
  api_desired_count = 2

  worker_cpu           = 2048
  worker_memory        = 4096
  worker_desired_count = 2

  worker_autoscaling_enabled    = true
  worker_autoscaling_min_count  = 2
  worker_autoscaling_max_count  = 20
  worker_autoscaling_cpu_target = 70

  google_client_id       = var.google_client_id
  google_client_secret   = var.google_client_secret
  google_allowed_domains = var.google_allowed_domains

  domain_name     = var.domain_name
  route53_zone_id = var.route53_zone_id

  worker_pool_token = var.worker_pool_token

  tags = {
    Owner = var.team_email
  }
}

variable "name"       { default = "stackd" }
variable "aws_region" { default = "eu-west-1" }
variable "team_email" { default = "" }

variable "google_client_id"       { sensitive = true }
variable "google_client_secret"   { sensitive = true }
variable "google_allowed_domains" { default = "" }

variable "domain_name"     {}
variable "route53_zone_id" { default = null }

variable "worker_pool_token" { sensitive = true }

output "public_url"                { value = module.stackd.public_url }
output "google_oauth_redirect_uri" { value = module.stackd.google_oauth_redirect_uri }
output "ecr_api_repository_url"    { value = module.stackd.ecr_api_repository_url }
output "ecr_worker_repository_url" { value = module.stackd.ecr_worker_repository_url }
output "cloudfront_distribution_id" { value = module.stackd.cloudfront_distribution_id }
output "ecs_cluster_name"           { value = module.stackd.ecs_cluster_name }
