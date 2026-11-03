variable "name" {
  type = string
}

variable "image_retention_count" {
  description = "Tagged images kept per repository. Keep it above the number of digests you may roll back to."
  type        = number
  default     = 30
}
