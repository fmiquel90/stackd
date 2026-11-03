output "data_key_arn" {
  value = aws_kms_key.data.arn
}

output "oidc_signing_key_arn" {
  value = aws_kms_key.oidc_signing.arn
}
