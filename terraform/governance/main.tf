terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }

  # Same state bucket as the other root modules, different prefix. Governance owns no
  # data and no compute, so it is deliberately separated from raw-storage: a mistake
  # while editing a budget must not be able to plan a change against the raw bucket.
  backend "gcs" {
    bucket = "splitsheet-tfstate-944054e7"
    prefix = "governance"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  # The Billing Budgets API refuses requests from user Application Default Credentials
  # unless a quota project is attached. Setting it here rather than relying on
  # `gcloud auth application-default set-quota-project` keeps the requirement in version
  # control, so a fresh clone works without an undocumented local step.
  billing_project       = var.project_id
  user_project_override = true
}

# Needed because a budget filter addresses projects by NUMBER, not by ID.
data "google_project" "this" {
  project_id = var.project_id
}

# Monthly spend alert.
#
# IMPORTANT, and stated here rather than only in docs because this is where someone will
# read it: a Cloud Billing budget is NOT a hard spending cap. It sends notifications when
# a threshold is crossed and does nothing else. It cannot stop a query, delete a cluster,
# or disable billing. Spend can and will exceed this figure if something runs away; the
# budget only shortens how long it takes to find out.
resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account_id
  display_name    = "splitsheet monthly budget (alert only, not a cap)"

  # Scoped to this project alone. Without this filter the budget would track the entire
  # billing account, including anything unrelated to SplitSheet.
  budget_filter {
    projects               = ["projects/${data.google_project.this.number}"]
    calendar_period        = "MONTH"
    credit_types_treatment = "INCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      # Must match the billing account currency, verified as BRL.
      currency_code = var.currency_code
      units         = var.monthly_amount_units
    }
  }

  # Early, actionable, and terminal. 50% is the "something changed" signal for a project
  # whose measured steady state is about R$0.30/month.
  threshold_rules {
    threshold_percent = 0.5
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 0.8
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "CURRENT_SPEND"
  }

  # all_updates_rule is deliberately omitted so notifications keep going to the billing
  # account's IAM administrators and users by default. Adding a Pub/Sub topic or a
  # monitoring channel would mean new resources and a new identity to manage, which this
  # phase explicitly excludes.
}
