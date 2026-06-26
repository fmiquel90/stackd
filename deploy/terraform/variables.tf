variable "name" {
  description = "Name prefix for all resources (e.g. \"stackd\" or \"acme-stackd\")"
  type        = string
  default     = "stackd"
}

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
}

variable "environment" {
  description = "Deployment environment tag (production, staging, …)"
  type        = string
  default     = "production"
}

variable "tags" {
  description = "Additional tags applied to every resource"
  type        = map(string)
  default     = {}
}

# ── Network ────────────────────────────────────────────────────────────────────

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "List of AZs to use. Minimum 2, recommended 3 for production."
  type        = list(string)
}

# ── Database ───────────────────────────────────────────────────────────────────

variable "db_instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t4g.medium"
}

variable "db_multi_az" {
  description = "Enable Multi-AZ standby for the RDS instance"
  type        = bool
  default     = false
}

variable "db_allocated_storage" {
  description = "Initial allocated storage in GiB"
  type        = number
  default     = 20
}

variable "db_max_allocated_storage" {
  description = "Upper limit for storage autoscaling in GiB. Set to 0 to disable autoscaling."
  type        = number
  default     = 100
}

# ── Compute ────────────────────────────────────────────────────────────────────

variable "api_cpu" {
  description = "CPU units for the API ECS task (1 vCPU = 1024)"
  type        = number
  default     = 512
}

variable "api_memory" {
  description = "Memory in MiB for the API ECS task"
  type        = number
  default     = 1024
}

variable "api_desired_count" {
  description = "Desired number of API ECS tasks"
  type        = number
  default     = 1
}

variable "worker_cpu" {
  description = "CPU units for each worker ECS task"
  type        = number
  default     = 1024
}

variable "worker_memory" {
  description = "Memory in MiB for each worker ECS task"
  type        = number
  default     = 2048
}

variable "worker_desired_count" {
  description = "Number of worker ECS tasks to run. Scale out by increasing this."
  type        = number
  default     = 1
}

# Images — leave null on first apply (ECR repos will exist but be empty).
# Push your images to ECR, then re-apply with the full URI.
variable "api_image" {
  description = "Full ECR image URI for the API (e.g. 123456789012.dkr.ecr.eu-west-1.amazonaws.com/stackd-api:v1.0.0)"
  type        = string
  default     = null
}

variable "worker_image" {
  description = "Full ECR image URI for the worker"
  type        = string
  default     = null
}

# ── Google OIDC ────────────────────────────────────────────────────────────────

variable "google_client_id" {
  description = "Google OAuth 2.0 Client ID. Leave empty to disable Google login."
  type        = string
  default     = ""
  sensitive   = true
}

variable "google_client_secret" {
  description = "Google OAuth 2.0 Client Secret."
  type        = string
  default     = ""
  sensitive   = true
}

variable "google_allowed_domains" {
  description = "Comma-separated Google Workspace domains (hd) allowed to sign in. Empty = any Google account."
  type        = string
  default     = ""
}

# ── Domain / TLS ───────────────────────────────────────────────────────────────

variable "domain_name" {
  description = "Custom domain for Stackd (e.g. \"stackd.acme.com\"). Null = use the CloudFront *.cloudfront.net domain."
  type        = string
  default     = null
}

variable "route53_zone_id" {
  description = "Route 53 hosted zone ID. Provide to automate ACM validation + CloudFront alias record."
  type        = string
  default     = null
}

variable "certificate_arn" {
  description = "Existing ACM certificate ARN (must be in us-east-1). Overrides domain_name for TLS."
  type        = string
  default     = null
}

# ── Workers ────────────────────────────────────────────────────────────────────

variable "worker_pool_token" {
  description = <<-EOT
    Pool authentication token for the worker fleet. Generate this in the Stackd UI after first
    login, then re-apply. See README bootstrap section. Stored in Secrets Manager.
  EOT
  type        = string
  default     = ""
  sensitive   = true
}
