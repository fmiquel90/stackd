data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

locals {
  region     = data.aws_region.current.region
  account_id = data.aws_caller_identity.current.account_id

  internal_domain_name = coalesce(var.internal_domain_name, "internal.${var.domain_name}")

  # The browser, the Google OAuth callback and the OIDC issuer (AWS STS fetches its JWKS here).
  public_url = "https://${var.domain_name}"
  # Workers reach the API (worker protocol, state backend) on the internal ALB, over HTTPS.
  internal_url = "https://${local.internal_domain_name}"

  # Bootstrap: until an API image is given, no ECS service, migration or worker is created.
  services_enabled = var.api_image != null
  workers_enabled  = local.services_enabled && var.worker_image != null && length(var.runner_images) > 0

  # Certificates: bring your own, or created (and validated through Route 53 when a zone is given).
  create_public_certificate   = var.certificate_arn == null
  create_internal_certificate = var.internal_certificate_arn == null
  route53_validation          = var.manage_dns_records != null ? var.manage_dns_records : var.route53_zone_id != null

  public_certificate_arn = (
    local.create_public_certificate ? one(aws_acm_certificate_validation.public[*].certificate_arn) : var.certificate_arn
  )
  internal_certificate_arn = (
    local.create_internal_certificate ? one(aws_acm_certificate_validation.internal[*].certificate_arn) : var.internal_certificate_arn
  )
}
