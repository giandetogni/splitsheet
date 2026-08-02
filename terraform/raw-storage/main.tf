provider "google" {
  project = var.project_id
  region  = var.region
  # Credentials come from Application Default Credentials only. No key file is
  # referenced here, and none may ever be committed.
}

# Second, independent copy of the preserved 2026-06 slice.
#
# The first copy is a local disk at 96% utilisation; the upstream artifact (full export
# 2593) is rotated by MetaBrainz. This bucket exists so that losing either one is
# survivable. Everything below is chosen to make accidental loss hard.
resource "google_storage_bucket" "raw" {
  name     = var.raw_bucket_name
  project  = var.project_id
  location = var.region

  storage_class = var.storage_class

  # No ACLs: IAM is the only access path, which makes permissions auditable in one place.
  uniform_bucket_level_access = true

  # Refuses to grant allUsers/allAuthenticatedUsers even if someone later tries.
  # The slice contains user_id, so public exposure is not merely untidy.
  public_access_prevention = "enforced"

  # Terraform must never be able to delete a non-empty bucket.
  force_destroy = false

  # Keeps a recoverable copy if an object is ever overwritten or deleted.
  versioning {
    enabled = true
  }

  labels = var.labels

  # Second, independent guard: even `terraform destroy` fails on this resource rather
  # than silently removing the data. Removing this block is a deliberate, reviewable act.
  lifecycle {
    prevent_destroy = true
  }
}

# No IAM bindings are created on purpose.
#
# Phase 1A uploads with the developer's own ADC identity, which already holds project
# permissions. Adding a service account now would create a long-lived identity with
# storage write access before anything needs it. A dedicated, narrowly scoped service
# account belongs to the phase that introduces automation, not to this one.
#
# Deliberately NOT applied here, and why:
#   * retention_policy -- would block overwriting an object, including legitimate
#     re-upload after a corrupted transfer. Locking it is irreversible. The combination
#     of versioning plus prevent_destroy gives recoverability without that trap.
#   * lifecycle rules -- nothing should ever expire or downgrade class in this bucket.
