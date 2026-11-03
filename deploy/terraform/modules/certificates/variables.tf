variable "domain_name" {
  type = string
}

variable "internal_domain_name" {
  type = string
}

variable "create_public" {
  type = bool
}

variable "create_internal" {
  type = bool
}

variable "route53_zone_id" {
  type    = string
  default = null
}
