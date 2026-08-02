terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }

  # Own prefix. This root grants an external system the right to act inside the project,
  # so it is kept apart from the roots that own data, spend and schema.
  backend "gcs" {
    bucket = "splitsheet-tfstate-944054e7"
    prefix = "ci-identity"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  billing_project       = var.project_id
  user_project_override = true
}

# Federated identity for GitHub Actions. No service-account key exists anywhere in this
# project: GitHub presents a short-lived OIDC token, STS exchanges it, and the resulting
# credential expires on its own.
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github-actions"
  project                   = var.project_id
  display_name              = "GitHub Actions"
  description               = "Federated identities for CI. Restricted to one repository."
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"
  project                            = var.project_id
  display_name                       = "GitHub OIDC"

  oidc {
    # The official GitHub Actions OIDC issuer. Anything else is not GitHub.
    issuer_uri = "https://token.actions.githubusercontent.com"
  }

  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository"          = "assertion.repository"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
    "attribute.ref"                 = "assertion.ref"
  }

  # Three independent pins, all by immutable numeric ID rather than by name.
  #
  # Names can be transferred or re-created: a repository called "splitsheet" could later
  # belong to someone else, and an account name can be renamed. repository_id and
  # repository_owner_id cannot be reassigned, so pinning those means a token minted for
  # any other repository or owner is rejected before it reaches an IAM check. The ref pin
  # limits it further to the default branch.
  attribute_condition = join(" && ", [
    "assertion.repository_owner_id == \"${var.github_repository_owner_id}\"",
    "assertion.repository_id == \"${var.github_repository_id}\"",
    "assertion.ref == \"refs/heads/${var.github_default_branch}\"",
  ])
}

# Identity the integration workflow runs as. Read-only by construction, see below.
resource "google_service_account" "ci_integration" {
  account_id   = "splitsheet-ci-integration"
  display_name = "SplitSheet CI (integration, read-only)"
  description  = "Runs read-only integration tests from GitHub Actions via WIF. Has no write permission on any dataset."
  project      = var.project_id
}

# Only this repository, on this branch, may impersonate the CI identity.
#
# The principalSet is scoped to attribute.repository_id, NOT to the whole pool. A
# pool-wide binding would let any future provider or repository added to the pool assume
# this identity, which defeats the point of pinning the provider.
resource "google_service_account_iam_member" "wif_can_impersonate_ci" {
  service_account_id = google_service_account.ci_integration.name
  role               = "roles/iam.workloadIdentityUser"
  member = join("", [
    "principalSet://iam.googleapis.com/projects/${var.project_number}",
    "/locations/global/workloadIdentityPools/",
    google_iam_workload_identity_pool.github.workload_identity_pool_id,
    "/attribute.repository_id/${var.github_repository_id}",
  ])
}

# --- least privilege for the CI identity ------------------------------------------------
# Deliberately absent: bigquery.user (can create datasets), bigquery.dataEditor,
# bigquery.admin, any storage write role, and any project-level dataViewer.

# Run query jobs. This alone grants no access to any data.
resource "google_project_iam_member" "ci_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.ci_integration.email}"
}

# Dataset metadata only, which is what INFORMATION_SCHEMA.COLUMNS needs. Grants no rows.
resource "google_bigquery_dataset_iam_member" "ci_metadata" {
  for_each   = toset(["splitsheet_bronze", "splitsheet_eval"])
  project    = var.project_id
  dataset_id = each.value
  role       = "roles/bigquery.metadataViewer"
  member     = "serviceAccount:${google_service_account.ci_integration.email}"
}

# Row access on exactly the four tables the read-only suite reads, table by table rather
# than dataset-wide, so a table added later is not readable by default.
resource "google_bigquery_table_iam_member" "ci_reads_bronze_tables" {
  for_each   = toset(["bronze_listens", "ext_listens_2026_06", "v_matcher_input"])
  project    = var.project_id
  dataset_id = "splitsheet_bronze"
  table_id   = each.value
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.ci_integration.email}"
}

# CI must read the label table to assert bronze and labels stay aligned. This is CI, not
# the matcher: the evaluation firewall constrains the matcher identity, and CI's job is
# precisely to verify that constraint from outside.
resource "google_bigquery_table_iam_member" "ci_reads_labels" {
  project    = var.project_id
  dataset_id = "splitsheet_eval"
  table_id   = "mapper_reference_labels"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.ci_integration.email}"
}

# Needed only because the existing suite asserts the matcher is denied access. Without it
# the firewall assertions could not run in CI at all.
resource "google_service_account_iam_member" "ci_can_impersonate_matcher" {
  service_account_id = var.matcher_service_account_id
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.ci_integration.email}"
}

# Read access to the raw objects, scoped to the one bucket.
#
# NECESSITY DEMONSTRATED, not assumed. The reconciliation test must recompute
# 39,200,000 = 38,199,641 + 1,000,359 from the external table, and a plain (non-BigLake)
# external table is read using the CALLER's credentials. Without this the query fails with:
#
#   Access Denied: BigQuery: Permission denied while globbing file pattern.
#   splitsheet-ci-integration@... does not have storage.objects.get access to
#   .../period=2026-06/1495.parquet
#
# (GitHub Actions run 30757048973.) The alternative considered and rejected was to assert
# a pre-recorded count instead, which would stop detecting source drift -- the exact thing
# the reconciliation exists to catch.
#
# This is objectViewer on this bucket only: read, no write, no delete, no other bucket. It
# does not weaken the evaluation firewall, which constrains the MATCHER identity; CI's
# purpose is to verify that constraint from the outside.
resource "google_storage_bucket_iam_member" "ci_reads_raw_objects" {
  bucket = var.raw_bucket_name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.ci_integration.email}"
}
