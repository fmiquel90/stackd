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

variable "manage_records" {
  description = "Create the records in route53_zone_id (decided by the root module, known at plan time)."
  type        = bool
  default     = false
}
