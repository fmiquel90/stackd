# ── Stackd application secrets ─────────────────────────────────────────────────

resource "random_bytes" "encryption_key" {
  length = 32
}

resource "random_bytes" "jwt_secret" {
  length = 32
}

resource "aws_secretsmanager_secret" "encryption_key" {
  name       = "${var.name}/encryption-key"
  kms_key_id = var.kms_key_arn
}

resource "aws_secretsmanager_secret_version" "encryption_key" {
  secret_id     = aws_secretsmanager_secret.encryption_key.id
  secret_string = random_bytes.encryption_key.base64
}

resource "aws_secretsmanager_secret" "jwt_secret" {
  name       = "${var.name}/jwt-secret"
  kms_key_id = var.kms_key_arn
}

resource "aws_secretsmanager_secret_version" "jwt_secret" {
  secret_id     = aws_secretsmanager_secret.jwt_secret.id
  secret_string = random_bytes.jwt_secret.base64
}

resource "aws_secretsmanager_secret" "database_url" {
  name       = "${var.name}/database-url"
  kms_key_id = var.kms_key_arn
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = var.database_url
}

# Google OIDC credentials — stored as a JSON object so the ECS task can reference each field
resource "aws_secretsmanager_secret" "google_credentials" {
  count = var.google_client_id != "" ? 1 : 0

  name       = "${var.name}/google-credentials"
  kms_key_id = var.kms_key_arn
}

resource "aws_secretsmanager_secret_version" "google_credentials" {
  count = var.google_client_id != "" ? 1 : 0

  secret_id = aws_secretsmanager_secret.google_credentials[0].id
  secret_string = jsonencode({
    client_id     = var.google_client_id
    client_secret = var.google_client_secret
  })
}

# Worker pool token — written here so ECS can inject it; updated by the operator post-bootstrap
resource "aws_secretsmanager_secret" "worker_pool_token" {
  name       = "${var.name}/worker-pool-token"
  kms_key_id = var.kms_key_arn
}

resource "aws_secretsmanager_secret_version" "worker_pool_token" {
  secret_id     = aws_secretsmanager_secret.worker_pool_token.id
  secret_string = var.worker_pool_token
}
