output "public_certificate_arn" {
  value = try(aws_acm_certificate.public[0].arn, null)
}

output "internal_certificate_arn" {
  value = try(aws_acm_certificate.internal[0].arn, null)
}

output "public_validation_fqdns" {
  value = [for d, r in aws_route53_record.validation : r.fqdn if d == var.domain_name]
}

output "internal_validation_fqdns" {
  value = [for d, r in aws_route53_record.validation : r.fqdn if d == var.internal_domain_name]
}

output "validation_records" {
  description = "DNS records that validate the certificates (create them yourself without a Route 53 zone)."
  value       = [
    for d in sort(tolist(local.validation_domains)) : {
      for_domain = d
      name       = local.validation_options[d].resource_record_name
      type       = local.validation_options[d].resource_record_type
      value      = local.validation_options[d].resource_record_value
    }
  ]
}
