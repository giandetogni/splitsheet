terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }

  # Remote state in the bucket created by terraform/bootstrap-state, under a prefix
  # unique to this root module. The prefix is what keeps this module's state from ever
  # being read or overwritten by another root module sharing the same bucket.
  #
  # This module owns the only irrecoverable asset in the project, so its state stays
  # isolated from every other module. The backing bucket is versioned with a 30-day
  # soft-delete window -- the recovery that the previous local-only file did not have.
  backend "gcs" {
    bucket = "splitsheet-tfstate-944054e7"
    prefix = "raw-storage"
  }
}
