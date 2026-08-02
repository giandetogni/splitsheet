variable "project_id" {
  description = "Existing GCP project. Created outside Terraform; see the bootstrap boundary in docs/runbook.md."
  type        = string
}

variable "region" {
  description = "Provider region. Service enablement is not regional."
  type        = string
  default     = "us-central1"
}

variable "enabled_apis" {
  description = "APIs the project uses today. Adding an unused API here would make this list a wish rather than a description, so entries are added by the phase that first needs them."
  type        = list(string)

  default = [
    # Required to manage any other service enablement.
    "serviceusage.googleapis.com",
    # Needed by the google_project data source and by project-level lookups.
    "cloudresourcemanager.googleapis.com",
    # Raw preservation bucket and Terraform state bucket.
    "storage.googleapis.com",
    # Billing account reads.
    "cloudbilling.googleapis.com",
    # The monthly budget in terraform/governance.
    "billingbudgets.googleapis.com",
    # Phase 2A: bronze datasets and the external table over the preserved slice.
    "bigquery.googleapis.com",
    # Phase 2B: the matcher service account (evaluation firewall).
    "iam.googleapis.com",
    # Phase 2B: impersonation, so the matcher identity needs no JSON key.
    "iamcredentials.googleapis.com",
  ]
}
