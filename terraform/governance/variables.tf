variable "project_id" {
  description = "Project the budget is filtered to. The budget tracks this project only."
  type        = string
}

variable "region" {
  description = "Provider region. The budget itself is not regional."
  type        = string
  default     = "us-central1"
}

variable "billing_account_id" {
  description = "Billing account that owns the budget, e.g. 01D939-A7BDBF-1BD0A5."
  type        = string
}

variable "currency_code" {
  description = "Must equal the billing account currency; a mismatch is rejected by the API. Verified as BRL via 'gcloud billing accounts describe'."
  type        = string
  default     = "BRL"
}

variable "monthly_amount_units" {
  description = "Whole currency units per month. 50 = R$50."
  type        = string
  default     = "50"
}
