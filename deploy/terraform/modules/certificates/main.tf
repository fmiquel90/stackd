# ACM certificates, created but NOT waited on: the validations live in the root module, so a
# first `apply -target=module.<stackd>.module.certificates` creates them and prints the DNS
# records to add when there is no Route 53 zone (the two-step bootstrap of the guide).
#
#   public    domain_name, in us-east-1 (CloudFront viewer certificate)
#   internal  internal_domain_name, in the deployment region (internal ALB: CloudFront's VPC
#             origin and the workers connect over HTTPS and verify it)

resource "aws_acm_certificate" "public" {
  count    = var.create_public ? 1 : 0
  provider = aws.us_east_1

  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate" "internal" {
  count = var.create_internal ? 1 : 0

  domain_name       = var.internal_domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

locals {
  # domain → its validation record. ACM gives the same record for the same name in the same
  # account, whatever the region, so the two certificates never conflict.
  validation_options = merge(
    { for o in try(aws_acm_certificate.public[0].domain_validation_options, []) : o.domain_name => o },
    { for o in try(aws_acm_certificate.internal[0].domain_validation_options, []) : o.domain_name => o },
  )
  # Keyed by names known at plan time (for_each needs them).
  validation_domains = toset(concat(
    var.create_public ? [var.domain_name] : [],
    var.create_internal ? [var.internal_domain_name] : [],
  ))
}

resource "aws_route53_record" "validation" {
  for_each = var.manage_records ? local.validation_domains : toset([])

  zone_id         = var.route53_zone_id
  name            = local.validation_options[each.key].resource_record_name
  type            = local.validation_options[each.key].resource_record_type
  records         = [local.validation_options[each.key].resource_record_value]
  ttl             = 300
  allow_overwrite = true
}
