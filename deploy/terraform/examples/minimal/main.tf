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

  # Uncomment to store state in S3 after bootstrap:
  # backend "s3" {
  #   bucket = "my-tfstate-bucket"
  #   key    = "stackd/minimal/terraform.tfstate"
  #   region = "eu-west-1"
  # }
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
  ]

  db_instance_class    = "db.t4g.micro"
  db_multi_az          = false
  db_allocated_storage = 20

  api_cpu    = 256
  api_memory = 512

  worker_cpu    = 512
  worker_memory = 1024

  google_client_id     = var.google_client_id
  google_client_secret = var.google_client_secret
  google_allowed_domains = var.google_allowed_domains

  domain_name     = var.domain_name
  route53_zone_id = var.route53_zone_id

  worker_pool_token = var.worker_pool_token
}

variable "name"       { default = "stackd" }
variable "aws_region" { default = "eu-west-1" }

variable "google_client_id"       { default = ""; sensitive = true }
variable "google_client_secret"   { default = ""; sensitive = true }
variable "google_allowed_domains" { default = "" }

variable "domain_name"     { default = null }
variable "route53_zone_id" { default = null }

variable "worker_pool_token" { default = ""; sensitive = true }

output "public_url"                { value = module.stackd.public_url }
output "google_oauth_redirect_uri" { value = module.stackd.google_oauth_redirect_uri }
output "ecr_api_repository_url"    { value = module.stackd.ecr_api_repository_url }
output "ecr_worker_repository_url" { value = module.stackd.ecr_worker_repository_url }
output "acm_validation_records"    { value = module.stackd.acm_validation_records }
