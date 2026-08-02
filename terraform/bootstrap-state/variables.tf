variable "project_id" {
  description = "Existing GCP project ID. Never created here."
  type        = string
}

variable "region" {
  description = "Single region for the state bucket. Same region as the raw bucket to keep everything in one failure and billing domain."
  type        = string
  default     = "us-central1"
}

variable "state_bucket_name" {
  description = "Globally unique name for the Terraform state bucket."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "state_bucket_name must be a valid GCS bucket name."
  }
}
