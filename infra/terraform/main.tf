# -----------------------------------------------------------------------------
# Google Cloud APIs Enablement
# -----------------------------------------------------------------------------
resource "google_project_service" "services" {
  for_each = toset([
    "run.googleapis.com",
    "bigquery.googleapis.com",
    "storage.googleapis.com",
    "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
  ])

  service            = each.key
  disable_on_destroy = false
}

# -----------------------------------------------------------------------------
# Service Accounts & Least-Privilege IAM
# -----------------------------------------------------------------------------
resource "google_service_account" "pipeline_runner" {
  account_id   = "txbar-pipeline-runner"
  display_name = "Service Account for Texas Bar Enrichment Cloud Run Job & Service"
  depends_on   = [google_project_service.services]
}

resource "google_project_iam_member" "runner_bq_editor" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.pipeline_runner.email}"
}

resource "google_project_iam_member" "runner_bq_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.pipeline_runner.email}"
}

resource "google_project_iam_member" "runner_storage_admin" {
  project = var.project_id
  role    = "roles/storage.objectAdmin"
  member  = "serviceAccount:${google_service_account.pipeline_runner.email}"
}

resource "google_project_iam_member" "runner_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.pipeline_runner.email}"
}

# -----------------------------------------------------------------------------
# Google Cloud Storage (Medallion Raw NDJSON Layer)
# -----------------------------------------------------------------------------
resource "google_storage_bucket" "raw_lake" {
  name                        = "${var.project_id}-txbar-raw"
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  depends_on = [google_project_service.services]
}

# -----------------------------------------------------------------------------
# BigQuery Dataset & Tables (Driven by /schemas/*.json single source of truth)
# -----------------------------------------------------------------------------
resource "google_bigquery_dataset" "txbar" {
  dataset_id  = var.dataset_id
  description = "Texas Bar directory enrichment and audit dataset"
  location    = var.region

  depends_on = [google_project_service.services]
}

resource "google_bigquery_table" "attorneys_source" {
  dataset_id = google_bigquery_dataset.txbar.dataset_id
  table_id   = "attorneys_source"
  schema     = file("${path.module}/../../schemas/attorneys_source.json")
}

resource "google_bigquery_table" "match_candidates" {
  dataset_id = google_bigquery_dataset.txbar.dataset_id
  table_id   = "match_candidates"
  schema     = file("${path.module}/../../schemas/match_candidates.json")

  time_partitioning {
    type  = "DAY"
    field = "scraped_at"
  }
}

resource "google_bigquery_table" "match_results" {
  dataset_id = google_bigquery_dataset.txbar.dataset_id
  table_id   = "match_results"
  schema     = file("${path.module}/../../schemas/match_results.json")

  time_partitioning {
    type  = "DAY"
    field = "scraped_at"
  }

  clustering = ["review_status", "firm_changed", "match_status"]
}

resource "google_bigquery_table" "review_events" {
  dataset_id = google_bigquery_dataset.txbar.dataset_id
  table_id   = "review_events"
  schema     = file("${path.module}/../../schemas/review_events.json")

  time_partitioning {
    type  = "DAY"
    field = "created_at"
  }
}

resource "google_bigquery_table" "pipeline_runs" {
  dataset_id = google_bigquery_dataset.txbar.dataset_id
  table_id   = "pipeline_runs"
  schema     = file("${path.module}/../../schemas/pipeline_runs.json")
}

resource "google_bigquery_table" "attorneys_master" {
  dataset_id = google_bigquery_dataset.txbar.dataset_id
  table_id   = "attorneys_master"
  schema     = file("${path.module}/../../schemas/match_results.json")

  time_partitioning {
    type  = "DAY"
    field = "scraped_at"
  }

  clustering = ["attorney_uuid", "license_status"]
}

# BigQuery View for Review Queue
resource "google_bigquery_table" "v_review_queue" {
  dataset_id = google_bigquery_dataset.txbar.dataset_id
  table_id   = "v_review_queue"

  view {
    query          = "SELECT * FROM `${var.project_id}.${var.dataset_id}.match_results` WHERE review_status = 'pending_review'"
    use_legacy_sql = false
  }

  depends_on = [google_bigquery_table.match_results]
}

# -----------------------------------------------------------------------------
# Secret Manager for Reviewer Authentication
# -----------------------------------------------------------------------------
resource "google_secret_manager_secret" "reviewer_token" {
  secret_id = "txbar-reviewer-token"

  replication {
    auto {}
  }

  depends_on = [google_project_service.services]
}

resource "google_secret_manager_secret_version" "token_version" {
  secret      = google_secret_manager_secret.reviewer_token.id
  secret_data = var.reviewer_token
}

# -----------------------------------------------------------------------------
# Cloud Run Job (Batch Scraper / Enrichment Pipeline)
# -----------------------------------------------------------------------------
resource "google_cloud_run_v2_job" "enricher_job" {
  name     = "txbar-enricher-job"
  location = var.region

  template {
    template {
      service_account = google_service_account.pipeline_runner.email
      timeout         = "1800s" # 30 minutes

      containers {
        image = var.container_image
        args  = ["txbar", "run"]

        env {
          name  = "STORAGE_BACKEND"
          value = "bigquery"
        }
        env {
          name  = "GCP_PROJECT_ID"
          value = var.project_id
        }
        env {
          name  = "BQ_DATASET"
          value = var.dataset_id
        }
        env {
          name  = "GCS_RAW_BUCKET"
          value = google_storage_bucket.raw_lake.name
        }

        resources {
          limits = {
            cpu    = "1000m"
            memory = "512Mi"
          }
        }
      }
    }
  }

  depends_on = [google_project_service.services]
}

# -----------------------------------------------------------------------------
# Cloud Scheduler (Triggers the Cloud Run Job)
# -----------------------------------------------------------------------------
resource "google_cloud_scheduler_job" "cron_trigger" {
  name             = "txbar-enricher-cron"
  description      = "Trigger weekly Texas Bar attorney enrichment batch job"
  schedule         = var.cron_schedule
  time_zone        = "UTC"
  attempt_deadline = "320s"

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.enricher_job.name}:run"

    oauth_token {
      service_account_email = google_service_account.pipeline_runner.email
    }
  }

  depends_on = [google_cloud_run_v2_job.enricher_job]
}

# -----------------------------------------------------------------------------
# Cloud Run Service (Human-in-the-Loop Review Dashboard)
# -----------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "review_console" {
  name     = "txbar-review-console"
  location = var.region

  template {
    service_account = google_service_account.pipeline_runner.email

    containers {
      image = var.container_image
      args  = ["txbar", "ui", "--host", "0.0.0.0", "--port", "8080"]

      env {
        name  = "STORAGE_BACKEND"
        value = "bigquery"
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "BQ_DATASET"
        value = var.dataset_id
      }
      env {
        name = "REVIEWER_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.reviewer_token.secret_id
            version = "latest"
          }
        }
      }

      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
      }
    }
  }

  depends_on = [google_project_service.services]
}

# Allow authenticated reviewer access or public with token
resource "google_cloud_run_v2_service_iam_member" "public_access" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.review_console.name
  role     = "roles/run.invoker"
  member   = "allUsers" # Protected at application layer by X-Reviewer-Token
}
