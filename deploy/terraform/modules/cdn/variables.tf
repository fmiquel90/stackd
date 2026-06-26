variable "name" {
  type = string
}

variable "alb_arn" {
  description = "ARN of the internal ALB (VPC Origin target)"
  type        = string
}

variable "alb_dns_name" {
  description = "DNS name of the internal ALB (CloudFront origin domain)"
  type        = string
}

variable "spa_bucket_regional_domain_name" {
  type = string
}

variable "spa_oac_id" {
  description = "CloudFront Origin Access Control ID for the SPA S3 bucket"
  type        = string
}

# Domain / TLS
variable "domain_name" {
  type    = string
  default = null
}

variable "route53_zone_id" {
  type    = string
  default = null
}

variable "certificate_arn" {
  description = "Pre-existing ACM certificate ARN (us-east-1). Overrides domain_name."
  type        = string
  default     = null
}

variable "price_class" {
  description = "CloudFront price class"
  type        = string
  default     = "PriceClass_100" # North America + Europe
}
