# The whole module applied against MiniStack (a local AWS emulator), not a real account.
#
# What it proves: every resource, argument and reference is accepted by the AWS API shapes,
# the dependency graph applies and destroys cleanly, write-only secrets stay out of the state.
# What it cannot prove: real AWS behaviour (CloudFront VPC origin routing, IAM/KMS policy
# evaluation, EC2 user data on a real host, RDS engine availability). See README.md.
#
#   task tf:sim          # start MiniStack, apply, check the state, destroy
#   task tf:sim:down     # stop MiniStack

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
}

variable "endpoint" {
  description = "MiniStack gateway."
  type        = string
  default     = "http://localhost:4566"
}

variable "aws_region" {
  type    = string
  default = "eu-west-3"
}

locals {
  name = "stackd-sim"

  # Images are never pulled by MiniStack (no Docker socket): any well-formed digest will do.
  fake_digest = "sha256:${sha256("stackd-sim")}"
  registry    = "000000000000.dkr.ecr.${var.aws_region}.amazonaws.com"
}

provider "aws" {
  region     = var.aws_region
  access_key = "test"
  secret_key = "test"

  s3_use_path_style           = true
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = false

  endpoints {
    acm            = var.endpoint
    autoscaling    = var.endpoint
    cloudfront     = var.endpoint
    cloudwatch     = var.endpoint
    cloudwatchlogs = var.endpoint
    ec2            = var.endpoint
    ecr            = var.endpoint
    ecs            = var.endpoint
    elbv2          = var.endpoint
    iam            = var.endpoint
    kms            = var.endpoint
    rds            = var.endpoint
    route53        = var.endpoint
    s3             = var.endpoint
    s3control      = var.endpoint
    secretsmanager = var.endpoint
    ssm            = var.endpoint
    sts            = var.endpoint
  }

  default_tags {
    tags = { Project = local.name, ManagedBy = "opentofu", Simulation = "ministack" }
  }
}

provider "aws" {
  alias      = "us_east_1"
  region     = "us-east-1"
  access_key = "test"
  secret_key = "test"

  s3_use_path_style           = true
  skip_credentials_validation = true
  skip_metadata_api_check     = true

  endpoints {
    acm            = var.endpoint
    cloudfront     = var.endpoint
    iam            = var.endpoint
    route53        = var.endpoint
    sts            = var.endpoint
    ec2            = var.endpoint
    s3             = var.endpoint
    s3control      = var.endpoint
    kms            = var.endpoint
    secretsmanager = var.endpoint
  }
}

# A zone in the emulator, so the certificates validate and the aliases are created by the
# module itself (the route53_zone_id path of the real bootstrap).
resource "aws_route53_zone" "sim" {
  name = "stackd-sim.example"
}

module "stackd" {
  source = "../../"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  name               = local.name
  availability_zones = ["${var.aws_region}a", "${var.aws_region}b"]

  domain_name     = "app.stackd-sim.example"
  route53_zone_id = aws_route53_zone.sim.zone_id
  # The zone is created here: its id is unknown at plan time (see the variable).
  manage_dns_records = true

  google_client_id       = "sim.apps.googleusercontent.com"
  google_allowed_domains = ["stackd-sim.example"]

  # Set, so the ECS services and the worker fleet are part of the graph.
  api_image    = "${local.registry}/${local.name}-api@${local.fake_digest}"
  worker_image = "${local.registry}/${local.name}-worker@${local.fake_digest}"
  runner_images = {
    "opentofu:1.12.2" = "${local.registry}/${local.name}-runner-opentofu@${local.fake_digest}"
  }

  # The migration task needs bash + the AWS CLI against a running cluster: not simulated.
  run_migrations = false

  # MiniStack has no public SSM AMI parameters.
  worker_ami_id = "ami-0123456789abcdef0"

  db_instance_class       = "db.t4g.micro"
  db_deletion_protection  = false
  alb_deletion_protection = false

  api_cpu           = 256
  api_memory        = 512
  api_desired_count = 1

  worker_count         = 1
  worker_instance_type = "t3.medium"
}

output "public_url" {
  value = module.stackd.public_url
}

output "ecr_repository_urls" {
  value = module.stackd.ecr_repository_urls
}

output "migrate_command" {
  value = module.stackd.migrate_command
}
