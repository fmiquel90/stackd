variable "name" {
  type = string
}

variable "kms_key_arn" {
  type = string
}

variable "saved_plan_retention_days" {
  description = "Safety net for saved plans the API did not delete (a run left unconfirmed). The API deletes each one when its run ends."
  type        = number
  default     = 90
}
