output "public_url" {
  description = "Stackd URL (open in browser)"
  value       = local.public_url
}

output "google_oauth_redirect_uri" {
  description = "Register this URI in Google Cloud Console → OAuth app → Authorized redirect URIs"
  value       = "${local.public_url}/api/v1/auth/google/callback"
}

output "acm_validation_records" {
  description = "Create these DNS records to validate your ACM certificate (only when route53_zone_id is not set)"
  value       = module.cdn.acm_validation_records
}

output "ecr_api_repository_url" {
  description = "Push the API image here: docker push <url>:TAG"
  value       = module.ecr.api_repository_url
}

output "ecr_worker_repository_url" {
  description = "Push the worker image here"
  value       = module.ecr.worker_repository_url
}

output "cloudfront_distribution_id" {
  description = "Used to invalidate the SPA cache after a frontend deploy: aws cloudfront create-invalidation --distribution-id ID --paths '/*'"
  value       = module.cdn.cloudfront_distribution_id
}

output "artifacts_bucket_id" {
  value = module.storage.artifacts_bucket_id
}

output "spa_bucket_id" {
  value = module.storage.spa_bucket_id
}

output "ecs_cluster_name" {
  value = module.api.cluster_name
}

output "db_endpoint" {
  description = "RDS endpoint (private — accessible only from within the VPC)"
  value       = module.database.endpoint
}
