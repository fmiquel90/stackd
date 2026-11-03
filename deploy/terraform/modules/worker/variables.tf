variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  description = "Worker subnets (outside the API's trusted-proxy range)."
  type        = list(string)
}

variable "api_url" {
  description = "HTTPS URL of the internal ALB."
  type        = string
}

variable "worker_image" {
  type = string
}

variable "runner_images" {
  type = map(string)
}

variable "ecr_repository_arns" {
  description = "Repositories the hosts may pull from."
  type        = list(string)
}

variable "pool_token_secret_arn" {
  type = string
}

variable "data_key_arn" {
  type = string
}

variable "worker_count" {
  type = number
}

variable "instance_type" {
  type = string
}

variable "architecture" {
  type = string
}

variable "ami_id" {
  type    = string
  default = null
}

variable "root_volume_size" {
  type = number
}

variable "max_concurrent_jobs" {
  type = number
}

variable "shutdown_timeout" {
  type = number
}

variable "docker_memory" {
  type    = string
  default = null
}

variable "docker_cpus" {
  type    = string
  default = null
}

variable "enable_ssm" {
  type = bool
}

variable "log_retention_days" {
  type = number
}
