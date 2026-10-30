"""Storage Abstraction Layer.

Supports two interchangeable backends:
1. `csv`: Local directory under data/warehouse/ storing CSV files that mirror BigQuery tables 1:1.
2. `bigquery`: Direct ingestion via BigQuery Load Jobs (NDJSON) and MERGE operations.

Both backends share identical contracts and schema conformity via schemas.py.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import polars as pl

from txbar.config import Settings, get_settings
from txbar.schemas import TABLES, conform, empty

logger = logging.getLogger(__name__)


class StorageBackend(Protocol):
    def seed_source(self, input_csv_path: Path) -> int: ...
    def load_source(self) -> pl.DataFrame: ...
    def save_run(
        self,
        run_record: dict[str, Any],
        candidates: list[dict[str, Any]],
        results: list[dict[str, Any]],
    ) -> None: ...
    def merge_master(self, run_id: str) -> int: ...
    def get_latest_run_id(self) -> str | None: ...
    def get_review_queue(self, run_id: str | None = None) -> pl.DataFrame: ...
    def get_all_results(self, run_id: str | None = None) -> pl.DataFrame: ...
    def get_candidates(self, attorney_uuid: str) -> pl.DataFrame: ...
    def apply_human_review(
        self,
        attorney_uuid: str,
        action: str,  # approve | reject | select_candidate
        chosen_contact_id: str | None,
        actor: str,
        note: str = "",
    ) -> dict[str, Any]: ...


class CsvStorageBackend:
    """Local CSV storage backend replicating BigQuery dataset tables 1:1."""

    def __init__(self, warehouse_dir: Path) -> None:
        self.warehouse_dir = warehouse_dir
        self.warehouse_dir.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def _table_path(self, table: str) -> Path:
        return self.warehouse_dir / f"{table}.csv"

    def _init_tables(self) -> None:
        for t in TABLES:
            p = self._table_path(t)
            if not p.exists():
                df = empty(t)
                df.write_csv(p)

    def _read_table(self, table: str) -> pl.DataFrame:
        p = self._table_path(table)
        if not p.exists() or p.stat().st_size == 0:
            return empty(table)
        try:
            df = pl.read_csv(p, infer_schema_length=10000)
            return conform(df, table)
        except Exception as e:
            logger.warning("Error reading %s: %s. Returning empty frame.", p, e)
            return empty(table)

    def _write_table(self, table: str, df: pl.DataFrame) -> None:
        conformed = conform(df, table)
        conformed.write_csv(self._table_path(table))

    def _append_table(self, table: str, df: pl.DataFrame) -> None:
        existing = self._read_table(table)
        combined = pl.concat([existing, df], how="vertical_relaxed")
        self._write_table(table, combined)

    def seed_source(self, input_csv_path: Path) -> int:
        """Seed `attorneys_source` from raw input CSV, creating immutable attorney_uuids."""
        raw_df = pl.read_csv(input_csv_path)
        # Standardize column names from "texas_admitted_lawyers_test_sheet - TX Lawyers.csv"
        # Name,Firm,Title,Practice,City,State,JD Year,Email
        col_map = {
            "Name": "name",
            "Firm": "firm",
            "Title": "title",
            "Practice": "practice",
            "City": "city",
            "State": "state",
            "JD Year": "jd_year",
            "Email": "email",
        }
        renamed = raw_df.rename({k: v for k, v in col_map.items() if k in raw_df.columns})

        now_utc = datetime.now(UTC)
        uuids = [str(uuid.uuid4()) for _ in range(len(renamed))]

        seeded = renamed.with_columns(
            pl.Series("attorney_uuid", uuids),
            pl.lit(None, dtype=pl.Utf8).alias("txbar_contact_id"),
            pl.lit(None, dtype=pl.Utf8).alias("bar_card_number"),
            pl.lit(None, dtype=pl.Datetime("us", "UTC")).alias("last_verified_at"),
            pl.lit(now_utc).alias("ingested_at"),
        )
        self._write_table("attorneys_source", seeded)
        return len(seeded)

    def load_source(self) -> pl.DataFrame:
        return self._read_table("attorneys_source")

    def save_run(
        self,
        run_record: dict[str, Any],
        candidates: list[dict[str, Any]],
        results: list[dict[str, Any]],
    ) -> None:
        if candidates:
            cand_df = pl.DataFrame(candidates)
            self._append_table("match_candidates", cand_df)

        if results:
            res_df = pl.DataFrame(results)
            self._append_table("match_results", res_df)

        run_df = pl.DataFrame([run_record])
        self._append_table("pipeline_runs", run_df)

    def merge_master(self, run_id: str) -> int:
        """Merge latest auto_approved or human_approved records into attorneys_master."""
        results = self._read_table("match_results")
        approved = results.filter(
            (pl.col("run_id") == run_id)
            & (pl.col("review_status").is_in(["auto_approved", "human_approved"]))
        )
        if len(approved) == 0:
            return 0

        # Master schema is a superset, or attorneys_source updated with golden fields
        master_path = self.warehouse_dir / "attorneys_master.csv"
        existing = (
            pl.read_csv(master_path, infer_schema_length=10000)
            if (master_path.exists() and master_path.stat().st_size > 0)
            else pl.DataFrame()
        )

        if len(existing) > 0 and "attorney_uuid" in existing.columns:
            # Upsert by attorney_uuid
            existing_remaining = existing.filter(~pl.col("attorney_uuid").is_in(approved["attorney_uuid"]))
            updated_master = pl.concat([existing_remaining, approved], how="vertical_relaxed")
        else:
            updated_master = approved

        updated_master.write_csv(master_path)
        return len(approved)

    def get_latest_run_id(self) -> str | None:
        """Return the most recent run_id from pipeline_runs or match_results."""
        runs = self._read_table("pipeline_runs")
        if len(runs) > 0 and "run_id" in runs.columns:
            return runs["run_id"].tail(1)[0]
        results = self._read_table("match_results")
        if len(results) > 0 and "run_id" in results.columns:
            return results["run_id"].tail(1)[0]
        return None

    def get_review_queue(self, run_id: str | None = None) -> pl.DataFrame:
        results = self.get_all_results(run_id=run_id)
        if len(results) == 0:
            return results
        return results.filter(pl.col("review_status") == "pending_review")

    def get_all_results(self, run_id: str | None = None) -> pl.DataFrame:
        results = self._read_table("match_results")
        if len(results) == 0:
            return results
        if run_id == "all":
            return results
        target_run_id = run_id or self.get_latest_run_id()
        if target_run_id and "run_id" in results.columns:
            filtered = results.filter(pl.col("run_id") == target_run_id)
            if len(filtered) > 0:
                return filtered
        if "attorney_uuid" in results.columns:
            return results.unique(subset=["attorney_uuid"], keep="last")
        return results

    def get_candidates(self, attorney_uuid: str) -> pl.DataFrame:
        cands = self._read_table("match_candidates")
        return cands.filter(pl.col("attorney_uuid") == attorney_uuid)

    def apply_human_review(
        self,
        attorney_uuid: str,
        action: str,
        chosen_contact_id: str | None,
        actor: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Apply a reviewer decision (approve, reject, pick alternative candidate)."""
        results = self._read_table("match_results")
        row = results.filter(pl.col("attorney_uuid") == attorney_uuid)
        if len(row) == 0:
            raise KeyError(f"Attorney {attorney_uuid} not found in results.")

        curr = row.to_dicts()[0]
        prev_status = curr.get("review_status")
        prev_contact = curr.get("contact_id")

        new_status = "human_approved" if action in ("approve", "select_candidate") else "human_rejected"
        final_contact = chosen_contact_id if chosen_contact_id else prev_contact

        now_utc = datetime.now(UTC)

        # Update in match_results
        updated_results = results.with_columns(
            pl.when(pl.col("attorney_uuid") == attorney_uuid)
            .then(pl.lit(new_status))
            .otherwise(pl.col("review_status"))
            .alias("review_status"),
            pl.when(pl.col("attorney_uuid") == attorney_uuid)
            .then(pl.lit(actor))
            .otherwise(pl.col("reviewed_by"))
            .alias("reviewed_by"),
            pl.when(pl.col("attorney_uuid") == attorney_uuid)
            .then(pl.lit(now_utc))
            .otherwise(pl.col("reviewed_at"))
            .alias("reviewed_at"),
            pl.when(pl.col("attorney_uuid") == attorney_uuid)
            .then(pl.lit(note))
            .otherwise(pl.col("review_note"))
            .alias("review_note"),
            pl.when((pl.col("attorney_uuid") == attorney_uuid) & (pl.lit(final_contact).is_not_null()))
            .then(pl.lit(final_contact))
            .otherwise(pl.col("contact_id"))
            .alias("contact_id"),
        )
        self._write_table("match_results", updated_results)

        # Log audit event
        event = {
            "event_id": str(uuid.uuid4()),
            "attorney_uuid": attorney_uuid,
            "run_id": curr.get("run_id"),
            "action": action,
            "previous_status": prev_status,
            "new_status": new_status,
            "previous_contact_id": prev_contact,
            "new_contact_id": final_contact,
            "actor": actor,
            "note": note,
            "created_at": now_utc,
        }
        self._append_table("review_events", pl.DataFrame([event]))

        # Feedback loop: cache contact_id back into attorneys_source for next run!
        if new_status == "human_approved" and final_contact:
            src = self._read_table("attorneys_source")
            updated_src = src.with_columns(
                pl.when(pl.col("attorney_uuid") == attorney_uuid)
                .then(pl.lit(final_contact))
                .otherwise(pl.col("txbar_contact_id"))
                .alias("txbar_contact_id"),
                pl.when(pl.col("attorney_uuid") == attorney_uuid)
                .then(pl.lit(now_utc))
                .otherwise(pl.col("last_verified_at"))
                .alias("last_verified_at"),
            )
            self._write_table("attorneys_source", updated_src)

            # Also update golden master
            self.merge_master(curr.get("run_id") or "")

        return event


class BigQueryStorageBackend:
    """BigQuery storage backend using Load Jobs and parameterized atomic MERGE DML."""

    def __init__(self, project_id: str, dataset_id: str) -> None:
        from google.cloud import bigquery

        self.bigquery = bigquery
        self.project_id = project_id
        self.dataset_id = dataset_id
        self.client = self._create_bq_client(project_id)

    def _create_bq_client(self, project_id: str) -> Any:
        from google.auth.exceptions import DefaultCredentialsError

        try:
            return self.bigquery.Client(project=project_id)
        except DefaultCredentialsError:
            import shutil
            import subprocess

            from google.oauth2.credentials import Credentials

            gcloud_bin = shutil.which("gcloud")
            if gcloud_bin:
                try:
                    token = subprocess.check_output([gcloud_bin, "auth", "print-access-token"], text=True).strip()
                    if token:
                        creds = Credentials(token=token)
                        return self.bigquery.Client(project=project_id, credentials=creds)
                except Exception as exc:
                    logger.debug("Failed to obtain gcloud access token: %s", exc)
            raise

    def _table_ref(self, table: str) -> str:
        return f"{self.project_id}.{self.dataset_id}.{table}"

    def ensure_tables(self, location: str = "us-central1") -> None:
        """Create the BigQuery dataset and all 6 tables from schemas/*.json if not present."""
        from txbar.schemas import TABLES, bq_schema

        dataset_ref = f"{self.project_id}.{self.dataset_id}"
        ds = self.bigquery.Dataset(dataset_ref)
        ds.location = location
        self.client.create_dataset(ds, exists_ok=True)

        for tbl_name in TABLES:
            raw_fields = bq_schema(tbl_name)
            schema_fields = [
                self.bigquery.SchemaField(
                    name=f["name"],
                    field_type=f["type"],
                    mode=f.get("mode", "NULLABLE"),
                    description=f.get("description", ""),
                )
                for f in raw_fields
            ]
            tbl = self.bigquery.Table(self._table_ref(tbl_name), schema=schema_fields)
            self.client.create_table(tbl, exists_ok=True)

    def _query_to_df(self, query: str, table: str, job_config: Any = None) -> pl.DataFrame:
        try:
            row_iter = self.client.query(query, job_config=job_config).result()
            rows = [dict(r.items()) for r in row_iter]
        except Exception as e:
            logger.warning("BigQuery query on %s returned error: %s", table, e)
            return empty(table)
        if not rows:
            return empty(table)
        return conform(pl.DataFrame(rows), table)

    def _load_rows(self, table: str, rows: list[dict[str, Any]], write_disposition: str = "WRITE_APPEND") -> None:
        if not rows:
            return
        import json

        from txbar.schemas import bq_schema

        cleaned = json.loads(json.dumps(rows, default=str))
        raw_fields = bq_schema(table)
        schema_fields = [
            self.bigquery.SchemaField(
                name=f["name"],
                field_type=f["type"],
                mode=f.get("mode", "NULLABLE"),
                description=f.get("description", ""),
            )
            for f in raw_fields
        ]
        job_config = self.bigquery.LoadJobConfig(
            schema=schema_fields,
            source_format=self.bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=write_disposition,
        )
        job = self.client.load_table_from_json(cleaned, self._table_ref(table), job_config=job_config)
        job.result()

    def seed_source(self, input_csv_path: Path) -> int:
        self.ensure_tables()
        raw_df = pl.read_csv(input_csv_path)
        col_map = {
            "Name": "name",
            "Firm": "firm",
            "Title": "title",
            "Practice": "practice",
            "City": "city",
            "State": "state",
            "JD Year": "jd_year",
            "Email": "email",
        }
        renamed = raw_df.rename({k: v for k, v in col_map.items() if k in raw_df.columns})
        now_utc = datetime.now(UTC)
        uuids = [str(uuid.uuid4()) for _ in range(len(renamed))]
        seeded = renamed.with_columns(
            pl.Series("attorney_uuid", uuids),
            pl.lit(None, dtype=pl.Utf8).alias("txbar_contact_id"),
            pl.lit(None, dtype=pl.Utf8).alias("bar_card_number"),
            pl.lit(None, dtype=pl.Datetime("us", "UTC")).alias("last_verified_at"),
            pl.lit(now_utc).alias("ingested_at"),
        )
        conformed = conform(seeded, "attorneys_source")
        self._load_rows("attorneys_source", conformed.to_dicts(), write_disposition="WRITE_TRUNCATE")
        return len(conformed)

    def load_source(self) -> pl.DataFrame:
        query = f"SELECT * FROM `{self._table_ref('attorneys_source')}`"
        return self._query_to_df(query, "attorneys_source")

    def save_run(
        self,
        run_record: dict[str, Any],
        candidates: list[dict[str, Any]],
        results: list[dict[str, Any]],
    ) -> None:
        self.ensure_tables()
        if candidates:
            cand_df = conform(pl.DataFrame(candidates), "match_candidates")
            self._load_rows("match_candidates", cand_df.to_dicts())
        if results:
            res_df = conform(pl.DataFrame(results), "match_results")
            self._load_rows("match_results", res_df.to_dicts())
        run_df = conform(pl.DataFrame([run_record]), "pipeline_runs")
        self._load_rows("pipeline_runs", run_df.to_dicts())

    def merge_master(self, run_id: str) -> int:
        query = f"""
        MERGE `{self._table_ref('attorneys_master')}` T
        USING (
            SELECT * FROM `{self._table_ref('match_results')}`
            WHERE run_id = @run_id AND review_status IN ('auto_approved', 'human_approved')
        ) S
        ON T.attorney_uuid = S.attorney_uuid
        WHEN MATCHED THEN
          UPDATE SET T.current_firm = S.current_firm, T.phone = S.phone, T.bar_card_number = S.bar_card_number,
                     T.license_status = S.license_status, T.scraped_at = S.scraped_at, T.review_status = S.review_status
        WHEN NOT MATCHED THEN
          INSERT ROW;
        """
        job_config = self.bigquery.QueryJobConfig(
            query_parameters=[self.bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
        )
        job = self.client.query(query, job_config=job_config)
        job.result()
        return job.num_dml_affected_rows or 0

    def get_latest_run_id(self) -> str | None:
        query = f"SELECT run_id FROM `{self._table_ref('pipeline_runs')}` ORDER BY started_at DESC LIMIT 1"
        try:
            rows = list(self.client.query(query).result())
            if rows:
                return str(rows[0]["run_id"])
        except Exception:
            pass
        return None

    def get_review_queue(self, run_id: str | None = None) -> pl.DataFrame:
        results = self.get_all_results(run_id=run_id)
        if len(results) == 0:
            return results
        return results.filter(pl.col("review_status") == "pending_review")

    def get_all_results(self, run_id: str | None = None) -> pl.DataFrame:
        target_run_id = run_id or self.get_latest_run_id()
        q = f"SELECT * FROM `{self._table_ref('match_results')}`"
        params = []
        if target_run_id and target_run_id != "all":
            q += " WHERE run_id = @run_id"
            params.append(self.bigquery.ScalarQueryParameter("run_id", "STRING", target_run_id))
        job_config = self.bigquery.QueryJobConfig(query_parameters=params)
        return self._query_to_df(q, "match_results", job_config=job_config)

    def get_candidates(self, attorney_uuid: str) -> pl.DataFrame:
        q = f"SELECT * FROM `{self._table_ref('match_candidates')}` WHERE attorney_uuid = @attorney_uuid"
        job_config = self.bigquery.QueryJobConfig(
            query_parameters=[self.bigquery.ScalarQueryParameter("attorney_uuid", "STRING", attorney_uuid)]
        )
        return self._query_to_df(q, "match_candidates", job_config=job_config)

    def apply_human_review(
        self,
        attorney_uuid: str,
        action: str,
        chosen_contact_id: str | None,
        actor: str,
        note: str = "",
    ) -> dict[str, Any]:
        now_utc = datetime.now(UTC)
        new_status = "human_approved" if action in ("approve", "select_candidate") else "human_rejected"
        update_query = f"""
        UPDATE `{self._table_ref('match_results')}`
        SET review_status = @new_status,
            reviewed_by = @actor,
            reviewed_at = @reviewed_at,
            review_note = @note,
            contact_id = COALESCE(@chosen_contact_id, contact_id)
        WHERE attorney_uuid = @attorney_uuid;
        """
        job_config = self.bigquery.QueryJobConfig(
            query_parameters=[
                self.bigquery.ScalarQueryParameter("new_status", "STRING", new_status),
                self.bigquery.ScalarQueryParameter("actor", "STRING", actor),
                self.bigquery.ScalarQueryParameter("reviewed_at", "TIMESTAMP", now_utc),
                self.bigquery.ScalarQueryParameter("note", "STRING", note),
                self.bigquery.ScalarQueryParameter("chosen_contact_id", "STRING", chosen_contact_id),
                self.bigquery.ScalarQueryParameter("attorney_uuid", "STRING", attorney_uuid),
            ]
        )
        self.client.query(update_query, job_config=job_config).result()

        latest_run_id = self.get_latest_run_id()
        event = {
            "event_id": str(uuid.uuid4()),
            "attorney_uuid": attorney_uuid,
            "run_id": latest_run_id,
            "action": action,
            "previous_status": "pending_review",
            "new_status": new_status,
            "previous_contact_id": None,
            "new_contact_id": chosen_contact_id,
            "actor": actor,
            "note": note,
            "created_at": now_utc,
        }
        event_df = conform(pl.DataFrame([event]), "review_events")
        self._load_rows("review_events", event_df.to_dicts())

        if new_status == "human_approved":
            if chosen_contact_id:
                cache_query = f"""
                UPDATE `{self._table_ref('attorneys_source')}`
                SET txbar_contact_id = @contact_id, last_verified_at = @verified_at
                WHERE attorney_uuid = @attorney_uuid;
                """
                cache_cfg = self.bigquery.QueryJobConfig(
                    query_parameters=[
                        self.bigquery.ScalarQueryParameter("contact_id", "STRING", chosen_contact_id),
                        self.bigquery.ScalarQueryParameter("verified_at", "TIMESTAMP", now_utc),
                        self.bigquery.ScalarQueryParameter("attorney_uuid", "STRING", attorney_uuid),
                    ]
                )
                self.client.query(cache_query, job_config=cache_cfg).result()
            if latest_run_id:
                self.merge_master(latest_run_id)

        return event


def get_storage(settings: Settings | None = None) -> StorageBackend:
    import os

    s = settings or get_settings()
    if s.google_application_credentials:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = s.google_application_credentials
    if s.storage_backend == "bigquery" and s.gcp_project_id:
        return BigQueryStorageBackend(project_id=s.gcp_project_id, dataset_id=s.bq_dataset)
    return CsvStorageBackend(warehouse_dir=s.resolve(s.local_warehouse_dir))
