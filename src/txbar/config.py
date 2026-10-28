"""Runtime configuration. Every knob is an env var so the same image runs locally and on Cloud Run."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    # Storage (Cloud-native BigQuery by default)
    storage_backend: Literal["bigquery", "csv"] = "bigquery"
    local_warehouse_dir: Path = PROJECT_ROOT / "data" / "warehouse"
    output_dir: Path = PROJECT_ROOT / "data" / "output"

    # GCP / BigQuery
    gcp_project_id: str | None = "txbar-pipeline-1027"
    gcp_region: str = "us-central1"
    bq_dataset: str = "txbar"
    gcs_raw_bucket: str | None = None
    google_application_credentials: str | None = None

    # Scraper politeness / resilience
    scraper_min_delay_s: float = 1.2
    scraper_max_delay_s: float = 2.8
    scraper_max_retries: int = 4
    scraper_timeout_s: float = 30.0
    scraper_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    scraper_max_detail_fetches: int = 4  # detail pages fetched per attorney (top-N candidates)

    # Matching thresholds
    match_auto_approve: float = 85.0
    match_review_floor: float = 55.0

    # Run-level data-quality gate
    dq_max_not_found_ratio: float = 0.20
    dq_max_missing_phone_ratio: float = 0.35

    # Review UI
    reviewer_token: str = "change-me"

    def resolve(self, p: Path) -> Path:
        return p if p.is_absolute() else PROJECT_ROOT / p


@lru_cache
def get_settings() -> Settings:
    return Settings()
