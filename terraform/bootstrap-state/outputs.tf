output "state_bucket_name" {
  description = "Bucket other root modules use as their GCS backend."
  value       = google_storage_bucket.tfstate.name
}

output "state_bucket_location" {
  value = google_storage_bucket.tfstate.location
}

output "backend_config_hint" {
  description = "Backend block other roots must declare, each with its own prefix."
  value       = "terraform { backend \"gcs\" { bucket = \"${google_storage_bucket.tfstate.name}\", prefix = \"<root-module-name>\" } }"
}
