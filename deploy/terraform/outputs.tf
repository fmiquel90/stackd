output "public_url" {
  description = "Stackd's URL (and the workload OIDC issuer)."
  value       = local.public_url
}

output "internal_url" {
  description = "URL the workers use (internal ALB)."
  value       = local.internal_url
}

output "google_oauth_redirect_uri" {
  description = "Register it in Google Cloud Console → OAuth client → Authorized redirect URIs."
  value       = "${local.public_url}/api/v1/auth/google/callback"
}

output "acm_validation_records" {
  description = "Certificate validation records to create when route53_zone_id is null."
  value       = [for r in module.certificates.validation_records : r if !local.route53_validation]
}

output "dns_records" {
  description = "Records to create when route53_zone_id is null."
  value = [
    for r in [
      { name = var.domain_name, type = "CNAME", value = module.cdn.distribution_domain_name },
      { name = local.internal_domain_name, type = "CNAME", value = module.api.alb_dns_name },
    ] : r if !local.route53_validation
  ]
}

output "ecr_repository_urls" {
  description = "Push the images here (api, worker, runner), then deploy them by digest."
  value       = module.ecr.repository_urls
}

output "google_client_secret_arn" {
  description = "Put the Google OAuth client secret here (aws secretsmanager put-secret-value) before the API starts."
  value       = module.secrets.google_client_secret_arn
}

output "worker_pool_token_secret_arn" {
  description = "Put the worker pool token here once the pool exists in Stackd; the worker hosts wait for it."
  value       = module.secrets.worker_pool_token_secret_arn
}

output "oidc_signing_key_arn" {
  description = "KMS key signing the workload OIDC tokens."
  value       = module.kms.oidc_signing_key_arn
}

output "spa_bucket" {
  description = "Upload the built front here (scripts/publish-spa.sh)."
  value       = module.storage.spa_bucket_id
}

output "cloudfront_distribution_id" {
  description = "Invalidated after an SPA upload (scripts/publish-spa.sh)."
  value       = module.cdn.distribution_id
}

output "artifacts_bucket" {
  value = module.storage.artifacts_bucket_id
}

output "ecs_cluster_name" {
  value = module.api.cluster_name
}

output "api_service_name" {
  value = module.api.service_name
}

output "api_log_group" {
  value = module.api.log_group_name
}

output "migrate_command" {
  description = "Runs the migration task by hand (run_migrations = false). Null until api_image is set."
  value       = module.api.migrate_command
}

output "worker_autoscaling_group" {
  value = try(module.worker[0].autoscaling_group_name, null)
}

output "worker_log_group" {
  value = try(module.worker[0].log_group_name, null)
}

output "db_endpoint" {
  description = "RDS endpoint (private)."
  value       = module.database.endpoint
}
