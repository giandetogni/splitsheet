output "enabled_apis" {
  description = "APIs under Terraform management in this project."
  value       = sort([for s in google_project_service.apis : s.service])
}
