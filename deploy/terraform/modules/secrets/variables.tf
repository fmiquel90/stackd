variable "name" {
  type = string
}

variable "kms_key_arn" {
  type = string
}

variable "google_client_id" {
  type      = string
  sensitive = true
}

variable "google_client_secret" {
  type      = string
  sensitive = true
}

variable "worker_pool_token" {
  type      = string
  sensitive = true
}

# Passed from the database module after RDS is provisioned
variable "database_url" {
  type      = string
  sensitive = true
}
