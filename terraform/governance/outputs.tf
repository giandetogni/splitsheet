output "budget_name" {
  description = "Resource name of the budget, for gcloud billing budgets describe."
  value       = google_billing_budget.monthly.name
}

output "budget_display_name" {
  value = google_billing_budget.monthly.display_name
}

output "budget_filtered_project_number" {
  description = "Budget tracks only this project number."
  value       = data.google_project.this.number
}
