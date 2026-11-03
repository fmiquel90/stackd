output "encryption_key_secret_arn" {
  value = aws_secretsmanager_secret.encryption_key.arn
}

output "jwt_secret_secret_arn" {
  value = aws_secretsmanager_secret.jwt_secret.arn
}

output "google_client_secret_arn" {
  value = aws_secretsmanager_secret.google_client_secret.arn
}

output "worker_pool_token_secret_arn" {
  value = aws_secretsmanager_secret.worker_pool_token.arn
}
