variable "name" {
  type = string
}

variable "domain_name" {
  type = string
}

variable "internal_domain_name" {
  type = string
}

variable "certificate_arn" {
  description = "Validated ACM certificate of domain_name, in us-east-1."
  type        = string
}

variable "route53_zone_id" {
  type    = string
  default = null
}

variable "alb_arn" {
  type = string
}

variable "spa_bucket_regional_domain_name" {
  type = string
}

variable "spa_oac_id" {
  type = string
}

variable "price_class" {
  type = string
}
