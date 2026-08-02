output "raw_bucket_name" {
  description = "Name of the raw preservation bucket."
  value       = google_storage_bucket.raw.name
}

output "raw_bucket_url" {
  description = "gs:// URL used by the upload and verification scripts."
  value       = google_storage_bucket.raw.url
}

output "raw_bucket_location" {
  description = "Region the bucket was actually created in, for the cost record."
  value       = google_storage_bucket.raw.location
}

output "raw_bucket_storage_class" {
  description = "Storage class actually applied."
  value       = google_storage_bucket.raw.storage_class
}

output "public_access_prevention" {
  description = "Must read 'enforced'."
  value       = google_storage_bucket.raw.public_access_prevention
}

output "uniform_bucket_level_access" {
  description = "Must read true."
  value       = google_storage_bucket.raw.uniform_bucket_level_access
}
