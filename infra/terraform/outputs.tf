output "review_console_url" {
  description = "Public URL for the Human-in-the-Loop Review Dashboard"
  value       = google_cloud_run_v2_service.review_console.uri
}

output "bigquery_dataset" {
  description = "BigQuery dataset ID"
  value       = google_bigquery_dataset.txbar.dataset_id
}

output "gcs_raw_lake_bucket" {
  description = "GCS Bucket storing raw NDJSON audit snapshots"
  value       = google_storage_bucket.raw_lake.name
}

output "cloud_run_job_name" {
  description = "Cloud Run batch job name"
  value       = google_cloud_run_v2_job.enricher_job.name
}
