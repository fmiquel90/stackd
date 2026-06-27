variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  description = "Private subnets for worker ECS tasks"
  type        = list(string)
}

variable "cluster_arn" {
  type = string
}

variable "cluster_name" {
  type = string
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
  default = 1024
}

variable "memory" {
  type    = number
  default = 2048
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "aws_region" {
  type = string
}

variable "environment_variables" {
  type    = map(string)
  default = {}
}

variable "secrets" {
  type      = map(string)
  default   = {}
  sensitive = true
}

# ── Autoscaling ────────────────────────────────────────────────────────────────

variable "autoscaling_enabled" {
  description = "Enable Application Auto Scaling for workers based on CPU utilization"
  type        = bool
  default     = false
}

variable "autoscaling_min_count" {
  description = "Minimum number of worker tasks when autoscaling is enabled"
  type        = number
  default     = 1
}

variable "autoscaling_max_count" {
  description = "Maximum number of worker tasks when autoscaling is enabled"
  type        = number
  default     = 10
}

variable "autoscaling_cpu_target" {
  description = "Target CPU utilization percentage (0–100) for the target-tracking policy"
  type        = number
  default     = 70
}
