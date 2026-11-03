variable "name" {
  description = "Name prefix for every resource (e.g. \"stackd\" or \"acme-stackd\")."
  type        = string
  default     = "stackd"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,22}$", var.name))
    error_message = "name must be 2-23 lowercase letters, digits or dashes, starting with a letter (it prefixes ALB and IAM names)."
  }
}

# ── Network ────────────────────────────────────────────────────────────────────

variable "vpc_cidr" {
  description = "CIDR block of the VPC (a /16 is carved into /24 subnets: public, private, workers)."
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "Availability zones to use: 2 minimum, 3 recommended in production."
  type        = list(string)

  validation {
    condition     = length(var.availability_zones) >= 2
    error_message = "At least two availability zones are required (the ALB and RDS need two)."
  }
}

# ── Domain / TLS ───────────────────────────────────────────────────────────────

variable "domain_name" {
  description = <<-EOT
    Public hostname of Stackd (e.g. "stackd.acme.com"), served by CloudFront. Required: it is the
    OIDC issuer URL your cloud accounts trust, so it must not change (a *.cloudfront.net name
    changes whenever the distribution is replaced).
  EOT
  type        = string

  validation {
    condition     = can(regex("^([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z]{2,}$", var.domain_name))
    error_message = "domain_name must be a lowercase DNS name such as stackd.acme.com."
  }
}

variable "internal_domain_name" {
  description = <<-EOT
    Hostname of the internal ALB, used by CloudFront (VPC origin, HTTPS) and by the workers.
    It resolves to private addresses. Default: "internal.<domain_name>".
  EOT
  type        = string
  default     = null
}

variable "route53_zone_id" {
  description = <<-EOT
    Public Route 53 hosted zone holding domain_name and internal_domain_name. When set, the
    certificate validation records and the aliases are created. When null, create them yourself
    (outputs `acm_validation_records` and `dns_records`) — see the guide's two-step bootstrap.
  EOT
  type        = string
  default     = null
}

variable "certificate_arn" {
  description = "Existing validated ACM certificate for domain_name, in us-east-1 (CloudFront). Null = one is created."
  type        = string
  default     = null
}

variable "internal_certificate_arn" {
  description = "Existing validated ACM certificate for internal_domain_name, in the deployment region (ALB). Null = one is created."
  type        = string
  default     = null
}

variable "cloudfront_price_class" {
  description = "CloudFront price class."
  type        = string
  default     = "PriceClass_100"
}

# ── Authentication ─────────────────────────────────────────────────────────────

variable "google_client_id" {
  description = "Google OAuth 2.0 client ID (not secret). The client secret is put into Secrets Manager out of band (output google_client_secret_arn)."
  type        = string

  validation {
    condition     = trimspace(var.google_client_id) != ""
    error_message = "google_client_id is required: Google sign-in is the only login of a production API."
  }
}

variable "google_allowed_domains" {
  description = <<-EOT
    Google Workspace domains (`hd` claim) allowed to sign in. Required and non-empty: an empty
    allowlist would admit ANY Google account, and the first account to sign in becomes admin.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.google_allowed_domains) > 0 && alltrue([for d in var.google_allowed_domains : can(regex("^[a-z0-9.-]+\\.[a-z]{2,}$", d))])
    error_message = "google_allowed_domains must list at least one domain (e.g. [\"acme.com\"])."
  }
}

# ── Images (by digest only) ────────────────────────────────────────────────────

variable "api_image" {
  description = <<-EOT
    API image pinned by digest: "<repo>@sha256:<64 hex>" (built from api/Dockerfile). Null on the
    bootstrap apply: the infrastructure is created without the ECS services and migrations.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.api_image == null || can(regex("^[^@:]+(:[0-9]+)?(/[^@:]+)*@sha256:[0-9a-f]{64}$", var.api_image))
    error_message = "api_image must be pinned by digest: <repo>@sha256:<digest> (a tag can be re-pointed)."
  }
}

variable "worker_image" {
  description = "Worker agent image pinned by digest (built from worker/Dockerfile). Null = no worker fleet."
  type        = string
  default     = null

  validation {
    condition     = var.worker_image == null || can(regex("^[^@:]+(:[0-9]+)?(/[^@:]+)*@sha256:[0-9a-f]{64}$", var.worker_image))
    error_message = "worker_image must be pinned by digest: <repo>@sha256:<digest>."
  }
}

variable "runner_images" {
  description = <<-EOT
    Runner images for the docker runner (deploy/images/), as STACKD_RUNNER_IMAGES:
    { "opentofu:1.12.2" = "<repo>@sha256:<digest>" }. A worker only takes runs whose stack's
    tool_version it has an image for. Required for the worker fleet.
  EOT
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for k, v in var.runner_images :
      can(regex("^(opentofu|terraform):[0-9A-Za-z.+-]+$", k)) && can(regex("^[^@:]+(:[0-9]+)?(/[^@:]+)*@sha256:[0-9a-f]{64}$", v))
    ])
    error_message = "runner_images keys are \"<opentofu|terraform>:<version>\", values images pinned by digest."
  }
}

# ── Database ───────────────────────────────────────────────────────────────────

variable "db_engine_version" {
  description = "RDS PostgreSQL version. 18 minimum: the migrations use the native uuidv7()."
  type        = string
  default     = "18"

  validation {
    condition     = tonumber(split(".", var.db_engine_version)[0]) >= 18
    error_message = "PostgreSQL 18 or later is required (uuidv7(), PLAN §6)."
  }
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.medium"
}

variable "db_multi_az" {
  description = "Multi-AZ standby for the RDS instance."
  type        = bool
  default     = false
}

variable "db_allocated_storage" {
  description = "Initial allocated storage in GiB."
  type        = number
  default     = 20
}

variable "db_max_allocated_storage" {
  description = "Storage autoscaling ceiling in GiB (0 disables autoscaling)."
  type        = number
  default     = 100
}

variable "db_deletion_protection" {
  description = "RDS deletion protection (and a final snapshot on delete)."
  type        = bool
  default     = true
}

variable "db_password_version" {
  description = "Bump to rotate the database password (a new random one is written to RDS and Secrets Manager); then roll the API service."
  type        = number
  default     = 1
}

variable "jwt_secret_version" {
  description = "Bump to rotate the session-signing secret (signs every user out); then roll the API service."
  type        = number
  default     = 1
}

# ── API ────────────────────────────────────────────────────────────────────────

variable "api_cpu" {
  description = "CPU units of the API task (1 vCPU = 1024)."
  type        = number
  default     = 512
}

variable "api_memory" {
  description = "Memory (MiB) of the API task."
  type        = number
  default     = 1024
}

variable "api_desired_count" {
  description = "Number of API tasks."
  type        = number
  default     = 2
}

variable "api_cpu_architecture" {
  description = "CPU architecture of the API image: X86_64 or ARM64."
  type        = string
  default     = "X86_64"
}

variable "run_migrations" {
  description = <<-EOT
    Run the migration task (`alembic upgrade head` + base seed) from the machine running
    tofu/terraform whenever the API task definition changes, before the service rolls. Needs
    bash and the AWS CLI there. false = run it yourself (output migrate_command) before rolling.
  EOT
  type        = bool
  default     = true
}

variable "alb_deletion_protection" {
  description = "Deletion protection on the internal ALB."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention of the API and worker logs."
  type        = number
  default     = 90
}

# ── Workers (EC2 + Docker, the `docker` runner) ────────────────────────────────

variable "worker_count" {
  description = "Number of worker hosts. Fixed: change it to scale (see the workers guide)."
  type        = number
  default     = 1
}

variable "worker_instance_type" {
  description = "Worker host instance type. Must match worker_architecture."
  type        = string
  default     = "t3.large"
}

variable "worker_architecture" {
  description = "Worker host architecture: x86_64 or arm64 (picks the Amazon Linux 2023 AMI)."
  type        = string
  default     = "x86_64"

  validation {
    condition     = contains(["x86_64", "arm64"], var.worker_architecture)
    error_message = "worker_architecture must be x86_64 or arm64."
  }
}

variable "worker_ami_id" {
  description = "Pin the worker AMI. Null = the latest Amazon Linux 2023 AMI at apply time (a new AMI rolls the hosts)."
  type        = string
  default     = null
}

variable "worker_root_volume_size" {
  description = "Worker root volume (GiB): images, workspaces, provider caches."
  type        = number
  default     = 50
}

variable "worker_max_concurrent_jobs" {
  description = "Jobs one worker runs at once (STACKD_MAX_CONCURRENT_JOBS), always on different environments."
  type        = number
  default     = 2
}

variable "worker_shutdown_timeout_seconds" {
  description = <<-EOT
    How long a stopping worker drains its running jobs (STACKD_SHUTDOWN_TIMEOUT_SECONDS) before
    it fails and kills them. Keep it above the API's apply budget (900 s by default). The
    termination lifecycle hook waits this long plus a margin (7200 s at most).
  EOT
  type        = number
  default     = 1800

  validation {
    condition     = var.worker_shutdown_timeout_seconds >= 60 && var.worker_shutdown_timeout_seconds <= 6600
    error_message = "worker_shutdown_timeout_seconds must be between 60 and 6600 (lifecycle hooks wait 7200 s at most)."
  }
}

variable "worker_docker_memory" {
  description = "Per-job-container memory limit (STACKD_DOCKER_MEMORY, e.g. \"2g\"). Null = no limit."
  type        = string
  default     = null
}

variable "worker_docker_cpus" {
  description = "Per-job-container CPU limit (STACKD_DOCKER_CPUS, e.g. \"1.5\"). Null = no limit."
  type        = string
  default     = null
}

variable "worker_enable_ssm" {
  description = "Attach AmazonSSMManagedInstanceCore to the worker hosts (Session Manager shell). Off by default: least privilege."
  type        = bool
  default     = false
}
