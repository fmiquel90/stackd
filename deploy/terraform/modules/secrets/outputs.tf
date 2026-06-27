output "encryption_key_secret_arn" {
  value = aws_secretsmanager_secret.encryption_key.arn
}

output "jwt_secret_secret_arn" {
  value = aws_secretsmanager_secret.jwt_secret.arn
}

output "database_url_secret_arn" {
  value = aws_secretsmanager_secret.database_url.arn
}

output "google_credentials_secret_arn" {
  value = var.google_client_id != "" ? aws_secretsmanager_secret.google_credentials[0].arn : null
}

output "worker_pool_token_secret_arn" {
  value = aws_secretsmanager_secret.worker_pool_token.arn
}

# All secret ARNs — used to build IAM policies
output "all_secret_arns" {
  value = compact([
    aws_secretsmanager_secret.encryption_key.arn,
    aws_secretsmanager_secret.jwt_secret.arn,
    aws_secretsmanager_secret.database_url.arn,
    var.google_client_id != "" ? aws_secretsmanager_secret.google_credentials[0].arn : null,
    aws_secretsmanager_secret.worker_pool_token.arn,
  ])
}
