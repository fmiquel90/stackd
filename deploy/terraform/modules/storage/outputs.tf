output "artifacts_bucket_id" {
  value = aws_s3_bucket.artifacts.id
}

output "artifacts_bucket_arn" {
  value = aws_s3_bucket.artifacts.arn
}

output "spa_bucket_id" {
  value = aws_s3_bucket.spa.id
}

output "spa_bucket_arn" {
  value = aws_s3_bucket.spa.arn
}

output "spa_bucket_regional_domain_name" {
  value = aws_s3_bucket.spa.bucket_regional_domain_name
}

output "spa_oac_id" {
  value = aws_cloudfront_origin_access_control.spa.id
}
