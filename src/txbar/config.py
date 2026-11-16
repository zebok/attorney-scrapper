"""Runtime configuration via environment variables (.env) with zero hardcoded paths or URLs."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


def find_project_root() -> Path:
    """Locate the project root containing schemas/ and data/ regardless of install mode."""
    if env_home := os.getenv("TXBAR_HOME"):
        candidate = Path(env_home).expanduser().resolve()
        if (candidate / "schemas").is_dir():
            return candidate

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "schemas").is_dir():
            return parent

    cwd = Path.cwd().resolve()
    if (cwd / "schemas").is_dir():
        return cwd

    return here.parents[2]


PROJECT_ROOT = find_project_root()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    # Storage
    storage_backend: Literal["bigquery", "csv"] = "csv"
    local_warehouse_dir: Path = Path("data/warehouse")
    output_dir: Path = Path("data/output")
    default_input_csv: Path = Path("data/input/tx_lawyers.csv")
    rules_dir: Path = Path("data/rules")
    schemas_dir: Path = Path("schemas")

    # GCP / BigQuery
    gcp_project_id: str | None = None
    gcp_region: str = "us-central1"
    bq_dataset: str = "txbar"
    google_application_credentials: str | None = None

    # Texas Bar directory endpoints
    txbar_base_url: str = "https://www.texasbar.com/AM/Template.cfm?Section=Find_A_Lawyer&Template="
    txbar_search_form_path: str = "/CustomSource/MemberDirectory/Search_Form_Client_Main.cfm"
    txbar_search_results_path: str = "/CustomSource/MemberDirectory/Result_form_client.cfm"
    txbar_detail_path: str = "/CustomSource/MemberDirectory/MemberDirectoryDetail.cfm&ContactID="

    # Scraper politeness / resilience
    scraper_min_delay_s: float = 1.2
    scraper_max_delay_s: float = 2.8
    scraper_max_retries: int = 4
    scraper_timeout_s: float = 30.0
    scraper_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    scraper_max_detail_fetches: int = 4

    # Matching weights and thresholds
    weight_name: float = 35.0
    weight_firm: float = 25.0
    weight_temporal: float = 25.0
    weight_geo: float = 15.0
    match_auto_approve: float = 85.0
    match_review_floor: float = 55.0
    match_ambiguity_delta: float = 8.0

    # Run-level data-quality gate
    dq_max_not_found_ratio: float = 0.20
    dq_max_missing_phone_ratio: float = 0.35

    # Review UI security (empty disables token auth for local dev)
    reviewer_token: str = ""

    def resolve(self, p: Path) -> Path:
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def search_form_url(self) -> str:
        return f"{self.txbar_base_url}{self.txbar_search_form_path}"

    @property
    def search_results_url(self) -> str:
        return f"{self.txbar_base_url}{self.txbar_search_results_path}"

    def detail_url(self, contact_id: str) -> str:
        return f"{self.txbar_base_url}{self.txbar_detail_path}{contact_id}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
