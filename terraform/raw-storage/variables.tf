variable "project_id" {
  description = "Existing GCP project ID. This module never creates the project: project creation needs org-level rights and is an administrative step performed once, outside Terraform."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid GCP project ID."
  }
}

variable "region" {
  description = "Single region for the raw bucket. Single-region (not multi-region) because the slice is a preservation copy read by batch jobs in the same region, and single-region storage is the cheaper tier with no cross-region replication we would be paying for."
  type        = string
  default     = "us-central1"
}

variable "raw_bucket_name" {
  description = "Globally unique name for the raw preservation bucket."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$", var.raw_bucket_name))
    error_message = "raw_bucket_name must be a valid GCS bucket name."
  }
}

variable "storage_class" {
  description = "STANDARD by default: the slice is only 2.49 GiB, so the monthly difference against NEARLINE is about USD 0.025, while NEARLINE adds a 30-day minimum storage duration and per-GB retrieval charges. Paying 2.5 cents to avoid retrieval fees and early-deletion penalties on an irrecoverable asset is the right trade."
  type        = string
  default     = "STANDARD"

  validation {
    condition     = contains(["STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE"], var.storage_class)
    error_message = "storage_class must be one of STANDARD, NEARLINE, COLDLINE, ARCHIVE."
  }
}

variable "labels" {
  description = "Labels applied to the bucket so storage cost can be attributed in billing export."
  type        = map(string)
  default = {
    project      = "splitsheet"
    phase        = "1a"
    content      = "raw-preserved-slice"
    provenance   = "listenbrainz-fullexport-cc0"
    contains_pii = "user-id"
  }
}
