locals {
  common_tags = merge(
    {
      Project     = var.name
      Environment = var.environment
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  # The public URL the browser (and the Google OAuth callback) use.
  # Known only after CloudFront is created when no custom domain is provided.
  public_url = var.domain_name != null ? "https://${var.domain_name}" : "https://${module.cdn.cloudfront_domain}"

  # URL workers use to reach the API — internal ALB, bypasses CloudFront.
  internal_url = "http://${module.api.alb_dns_name}"

  # Placeholders used on first apply before images are pushed to ECR.
  api_image    = coalesce(var.api_image, "${module.ecr.api_repository_url}:latest")
  worker_image = coalesce(var.worker_image, "${module.ecr.worker_repository_url}:latest")
}
