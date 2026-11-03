variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "enabled" {
  description = "Create the task definitions, the migration and the service (false on the bootstrap apply)."
  type        = bool
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  description = "Private subnets: the internal ALB and the API tasks."
  type        = list(string)
}

variable "certificate_arn" {
  description = "Validated ACM certificate of the internal hostname (HTTPS listener)."
  type        = string
}

variable "alb_deletion_protection" {
  type = bool
}

variable "execution_role_arn" {
  type = string
}

variable "task_role_arn" {
  type = string
}

variable "image" {
  type    = string
  default = null
}

variable "cpu" {
  type = number
}

variable "memory" {
  type = number
}

variable "cpu_architecture" {
  type = string
}

variable "desired_count" {
  type = number
}

variable "environment" {
  description = "Plain environment variables of the API container."
  type        = map(string)
}

variable "secrets" {
  description = "Environment variables injected from Secrets Manager: NAME → secret ARN."
  type        = map(string)
}

variable "run_migrations" {
  type = bool
}

variable "log_retention_days" {
  type = number
}
