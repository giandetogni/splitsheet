output "ci_service_account_email" {
  value = google_service_account.ci_integration.email
}

output "workload_identity_provider" {
  description = "Value for google-github-actions/auth."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "attribute_condition" {
  description = "The pins enforced on every federated token."
  value       = google_iam_workload_identity_pool_provider.github.attribute_condition
}
