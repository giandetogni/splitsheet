terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }

  # State for THIS module stays local, and that is not an oversight.
  #
  # This module creates the bucket that every other root module will use as its remote
  # backend. It cannot store its own state in a bucket it has not created yet. The
  # bootstrap module is therefore the one place where local state is correct; the file
  # it produces is small, changes almost never, and describes a single bucket that can
  # be re-imported by name if it is ever lost.
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Remote Terraform state for all other root modules.
#
# Holds no listen data, but it does describe every resource this project owns, so it is
# treated with the same care as the raw bucket: private, versioned, and awkward to delete.
resource "google_storage_bucket" "tfstate" {
  name     = var.state_bucket_name
  project  = var.project_id
  location = var.region

  storage_class = "STANDARD"

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  # State files are overwritten on every apply. Versioning is what makes a corrupted or
  # truncated write recoverable, so it is the single most important setting here.
  versioning {
    enabled = true
  }

  # Set explicitly rather than inherited: a deleted state object is recoverable for 30
  # days. The provider default is 7; relying on a default for a recovery window is how
  # people discover it was too short.
  soft_delete_policy {
    retention_duration_seconds = 2592000 # 30 days
  }

  # Versioning without expiry grows without bound: every apply leaves another archived
  # generation forever. These two rules keep enough history to recover from a bad write
  # while stopping the bucket becoming a landfill. They act ONLY on archived (noncurrent)
  # generations -- the live state object is never touched.
  lifecycle_rule {
    condition {
      with_state         = "ARCHIVED"
      num_newer_versions = 20
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      with_state                 = "ARCHIVED"
      days_since_noncurrent_time = 180
    }
    action {
      type = "Delete"
    }
  }

  labels = {
    project = "splitsheet"
    phase   = "1b"
    content = "terraform-state"
  }

  lifecycle {
    prevent_destroy = true
  }
}

# No service accounts are created in this phase. Access is the developer's own ADC
# identity; a dedicated identity belongs to the phase that introduces automation.
