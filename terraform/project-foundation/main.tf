terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }

  # Own prefix in the shared state bucket. This module is intentionally the widest in
  # blast radius per line of code -- disabling a service breaks everything using it --
  # so it stays isolated from the modules that own data and spend.
  backend "gcs" {
    bucket = "splitsheet-tfstate-944054e7"
    prefix = "project-foundation"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  # Service Usage and Cloud Billing reject user ADC without a quota project.
  billing_project       = var.project_id
  user_project_override = true
}

# The project's enabled APIs, declared so a clean project can be brought to the same
# state by Terraform alone.
#
# These five were originally enabled by hand with `gcloud services enable` while getting
# earlier phases working. That made the repository not reproducible from a clean project:
# a fresh clone would fail until someone repeated undocumented commands. This module
# closes that gap. The APIs already enabled were adopted into state by `terraform import`,
# never disabled and re-enabled.
#
# Scope rule: this list contains only what the project uses **today**. APIs for BigQuery,
# Dataproc and Composer are deliberately absent and belong to the phase that first needs
# them, so that the list stays an accurate description of the project rather than a wish.
resource "google_project_service" "apis" {
  for_each = toset(var.enabled_apis)

  project = var.project_id
  service = each.value

  # Never disable a service because Terraform is tearing down one root module. Disabling
  # an API is a project-wide act that would break the other roots -- and, for
  # serviceusage, would remove the ability to re-enable anything.
  disable_on_destroy = false

  # Equally, do not let a removal here cascade into dependent services.
  disable_dependent_services = false
}
