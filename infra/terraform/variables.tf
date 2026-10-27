variable "project_id" {
  description = "Google Cloud project ID"
  type        = string
}

variable "region" {
  description = "Google Cloud region for Cloud Run, BigQuery and GCS"
  type        = string
  default     = "us-central1"
}

variable "dataset_id" {
  description = "BigQuery dataset name"
  type        = string
  default     = "txbar"
}

variable "container_image" {
  description = "Artifact Registry or Container Registry image URL"
  type        = string
  default     = "gcr.io/txbar-attorney-pipeline/txbar:latest"
}

variable "reviewer_token" {
  description = "Secret authentication token for the review UI PATCH endpoint"
  type        = string
  sensitive   = true
  default     = "change-me-production-token"
}

variable "cron_schedule" {
  description = "Cloud Scheduler cron expression for batch enrichment job"
  type        = string
  default     = "0 6 * * 1" # Every Monday at 06:00 UTC
}
