# The API's secrets, none of them in the state, the plan or a tfvars file:
#
#   encryption-key        AES-256-GCM master key (SPECS §1, §13): 32 random bytes, generated as an
#                         ephemeral value and written once, write-only. NEVER rotate it this way:
#                         everything encrypted in the database would become unreadable.
#   jwt-secret            session-signing secret: ephemeral, write-only; bump the version to rotate
#                         (signs every user out).
#   google-client-secret  created empty: put the value from Google Cloud Console yourself
#                         (`aws secretsmanager put-secret-value`, see the guide) before the API starts.
#   worker-pool-token     created empty: put the token of the worker pool you create in Stackd; the
#                         worker hosts wait for it.

ephemeral "random_bytes" "encryption_key" {
  length = 32
}

resource "aws_secretsmanager_secret" "encryption_key" {
  name                    = "${var.name}/encryption-key"
  description             = "STACKD_ENCRYPTION_KEY: AES-256-GCM master key. Losing it loses every encrypted value."
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = 30

  # Destroying (or replacing) the master key loses every encrypted value: tearing a deployment
  # down means removing these two from the state first (see the guide).
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_secretsmanager_secret_version" "encryption_key" {
  secret_id        = aws_secretsmanager_secret.encryption_key.id
  secret_string_wo = ephemeral.random_bytes.encryption_key.base64
  # Fixed on purpose: a new key cannot decrypt what the old one encrypted (no re-encryption tool).
  secret_string_wo_version = 1

  lifecycle {
    prevent_destroy = true
  }
}

ephemeral "random_password" "jwt_secret" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "jwt_secret" {
  name                    = "${var.name}/jwt-secret"
  description             = "STACKD_JWT_SECRET: signs user sessions"
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "jwt_secret" {
  secret_id                = aws_secretsmanager_secret.jwt_secret.id
  secret_string_wo         = ephemeral.random_password.jwt_secret.result
  secret_string_wo_version = var.jwt_secret_version
}

resource "aws_secretsmanager_secret" "google_client_secret" {
  name                    = "${var.name}/google-client-secret"
  description             = "GOOGLE_CLIENT_SECRET of the ${var.name} API. Value set out of band."
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "worker_pool_token" {
  name                    = "${var.name}/worker-pool-token"
  description             = "Pool token of the ${var.name} worker fleet. Value set out of band."
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = 7
}
