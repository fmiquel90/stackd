variable "name" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "artifacts_bucket_arn" {
  type = string
}

variable "ecr_api_repository_arn" {
  type = string
}

variable "ecr_worker_repository_arn" {
  type = string
}

variable "secret_arns" {
  description = "ARNs of Secrets Manager secrets the tasks need to read"
  type        = list(string)
}

variable "kms_key_arn" {
  type = string
}
