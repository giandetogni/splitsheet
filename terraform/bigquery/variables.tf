variable "project_id" {
  type = string
}

variable "region" {
  description = "Must match the GCS bucket location; BigQuery cannot load or read externally across locations."
  type        = string
  default     = "us-central1"
}

variable "raw_bucket_name" {
  description = "Bucket holding the preserved slice. Managed by terraform/raw-storage; referenced here by name so the two roots stay decoupled."
  type        = string
}

variable "human_principal" {
  description = "Human identity permitted to impersonate the matcher service account."
  type        = string
}
