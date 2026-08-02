output "bronze_dataset" {
  value = google_bigquery_dataset.bronze.dataset_id
}

output "eval_dataset" {
  value = google_bigquery_dataset.eval.dataset_id
}

output "dataset_location" {
  value = google_bigquery_dataset.bronze.location
}
