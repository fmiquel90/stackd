output "cloudfront_domain" {
  value = aws_cloudfront_distribution.this.domain_name
}

output "cloudfront_distribution_arn" {
  value = aws_cloudfront_distribution.this.arn
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.this.id
}

output "acm_validation_records" {
  description = "DNS records to create in your provider to validate the ACM certificate (only when route53_zone_id is not set)"
  value = (local.create_cert && !local.use_r53_validation) ? [
    for o in aws_acm_certificate.this[0].domain_validation_options : {
      name  = o.resource_record_name
      type  = o.resource_record_type
      value = o.resource_record_value
    }
  ] : []
}
