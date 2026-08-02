variable "project_id" {
  type = string
}

variable "project_number" {
  description = "Numeric project number. Required because workload identity resource names use the number, not the ID."
  type        = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "github_repository_owner_id" {
  description = "Immutable numeric GitHub account ID. Pinned instead of the login, which can be renamed or transferred."
  type        = string
}

variable "github_repository_id" {
  description = "Immutable numeric GitHub repository ID. Pinned instead of the name, which can be re-created by someone else."
  type        = string
}

variable "github_default_branch" {
  description = "Only this branch may federate."
  type        = string
  default     = "main"
}

variable "matcher_service_account_id" {
  description = "Full resource name of the matcher SA, so CI can test that it is denied access."
  type        = string
}

variable "raw_bucket_name" {
  description = "Raw slice bucket. CI needs object read here so the reconciliation test can query the external table; see the necessity note in main.tf."
  type        = string
}
