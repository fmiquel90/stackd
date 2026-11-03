variable "name" {
  type = string
}

variable "api_secret_arns" {
  description = "Secrets the API task gets injected (and nothing else)."
  type        = list(string)
}

variable "data_key_arn" {
  description = "Symmetric KMS key of the secrets and of the artifacts bucket."
  type        = string
}

variable "oidc_signing_key_arn" {
  type = string
}

variable "artifacts_bucket_arn" {
  type = string
}
