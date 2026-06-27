variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  description = "Private subnets for ECS tasks and the internal ALB"
  type        = list(string)
}

variable "execution_role_arn" {
  type = string
}

variable "task_role_arn" {
  type = string
}

variable "image" {
  type = string
}

variable "cpu" {
  type    = number
  default = 512
}

variable "memory" {
  type    = number
  default = 1024
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "aws_region" {
  type = string
}

variable "environment_variables" {
  description = "Non-sensitive environment variables for the API container"
  type        = map(string)
  default     = {}
}

variable "secrets" {
  description = "Sensitive environment variables injected from Secrets Manager. Map of ENV_VAR_NAME → secret ARN (with optional :key:: suffix for JSON secrets)"
  type        = map(string)
  default     = {}
  sensitive   = true
}
