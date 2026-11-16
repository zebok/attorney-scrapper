"""Storage Abstraction Layer.

Supports two interchangeable backends sharing identical contracts via schemas.py:
1. `csv`: Local directory storing CSV tables mirroring BigQuery 1:1.
2. `bigquery`: Google Cloud BigQuery via NDJSON Load Jobs and parameterized MERGE/UPDATE DML.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import polars as pl

from txbar.config import Settings, get_settings
from txbar.schemas import TABLES, bq_schema, conform, empty

logger = logging.getLogger(__name__)

_SOURCE_COLUMN_MAP = {
    "Name": "name",
    "Firm": "firm",
    "Title": "title",
    "Practice": "practice",
    "City": "city",
    "State": "state",
    "JD Year": "jd_year",
    "Email": "email",
}


def prepare_source_dataframe(input_csv_path: Path) -> pl.DataFrame:
    """Read an input roster CSV and normalize it to the attorneys_source schema."""
    raw_df = pl.read_csv(input_csv_path)
    renamed = raw_df.rename({k: v for k, v in _SOURCE_COLUMN_MAP.items() if k in raw_df.columns})
    now_utc = datetime.now(UTC)
    uuids = [str(uuid.uuid4()) for _ in range(len(renamed))]
    seeded = renamed.with_columns(
        pl.Series("attorney_uuid", uuids),
        pl.lit(None, dtype=pl.Utf8).alias("txbar_contact_id"),
        pl.lit(None, dtype=pl.Utf8).alias("bar_card_number"),
        pl.lit(None, dtype=pl.Datetime("us", "UTC")).alias("last_verified_at"),
        pl.lit(now_utc).alias("ingested_at"),
    )
    return conform(seeded, "attorneys_source")


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
    def get_candidates(self, attorney_uuid: str, run_id: str | None = None) -> pl.DataFrame: ...
    def apply_human_review(
        self,
        attorney_uuid: str,
        action: str,
        chosen_contact_id: str | None,
        actor: str,
        note: str = "",
        run_id: str | None = None,
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
                empty(t).write_csv(p)

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
        conform(df, table).write_csv(self._table_path(table))

    def _append_table(self, table: str, df: pl.DataFrame) -> None:
        existing = self._read_table(table)
        combined = pl.concat([existing, conform(df, table)], how="vertical_relaxed")
        self._write_table(table, combined)

    def seed_source(self, input_csv_path: Path) -> int:
        seeded = prepare_source_dataframe(input_csv_path)
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
            self._append_table("match_candidates", pl.DataFrame(candidates))
        if results:
            self._append_table("match_results", pl.DataFrame(results))
        self._append_table("pipeline_runs", pl.DataFrame([run_record]))

    def merge_master(self, run_id: str) -> int:
        results = self._read_table("match_results")
        approved = results.filter(
            (pl.col("run_id") == run_id)
            & (pl.col("review_status").is_in(["auto_approved", "human_approved"]))
        )
        if len(approved) == 0:
            return 0

        existing = self._read_table("attorneys_master")
        if len(existing) > 0:
            remaining = existing.filter(~pl.col("attorney_uuid").is_in(approved["attorney_uuid"]))
            updated = pl.concat([remaining, approved], how="vertical_relaxed")
        else:
            updated = approved

        self._write_table("attorneys_master", updated)
        return len(approved)

    def get_latest_run_id(self) -> str | None:
        runs = self._read_table("pipeline_runs")
        if len(runs) > 0:
            return str(runs.sort("started_at")["run_id"].tail(1)[0])
        results = self._read_table("match_results")
        if len(results) > 0:
            return str(results["run_id"].tail(1)[0])
        return None

    def get_review_queue(self, run_id: str | None = None) -> pl.DataFrame:
        results = self.get_all_results(run_id=run_id)
        if len(results) == 0:
            return results
        return results.filter(pl.col("review_status") == "pending_review")

    def get_all_results(self, run_id: str | None = None) -> pl.DataFrame:
        results = self._read_table("match_results")
        if len(results) == 0 or run_id == "all":
            return results
        target_run_id = run_id or self.get_latest_run_id()
        if target_run_id:
            filtered = results.filter(pl.col("run_id") == target_run_id)
            if len(filtered) > 0:
                return filtered
        return results.unique(subset=["attorney_uuid"], keep="last")

    def get_candidates(self, attorney_uuid: str, run_id: str | None = None) -> pl.DataFrame:
        cands = self._read_table("match_candidates").filter(pl.col("attorney_uuid") == attorney_uuid)
        if len(cands) == 0 or run_id == "all":
            return cands
        target_run_id = run_id or self.get_latest_run_id()
        if target_run_id:
            return cands.filter(pl.col("run_id") == target_run_id)
        return cands

    def apply_human_review(
        self,
        attorney_uuid: str,
        action: str,
        chosen_contact_id: str | None,
        actor: str,
        note: str = "",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        results = self._read_table("match_results")
        target_run_id = run_id or self.get_latest_run_id()
        row_mask = pl.col("attorney_uuid") == attorney_uuid
        if target_run_id:
            row_mask = row_mask & (pl.col("run_id") == target_run_id)

        matched = results.filter(row_mask)
        if len(matched) == 0:
            raise KeyError(f"Attorney {attorney_uuid} not found in results for run {target_run_id}.")

        curr = matched.to_dicts()[-1]
        effective_run_id = str(curr.get("run_id") or target_run_id or "")
        prev_status = curr.get("review_status")
        prev_contact = curr.get("contact_id")

        new_status = "human_approved" if action in ("approve", "select_candidate") else "human_rejected"
        final_contact = chosen_contact_id if chosen_contact_id else prev_contact
        now_utc = datetime.now(UTC)

        target_mask = (pl.col("attorney_uuid") == attorney_uuid) & (pl.col("run_id") == effective_run_id)
        updated_results = results.with_columns(
            pl.when(target_mask)
            .then(pl.lit(new_status))
            .otherwise(pl.col("review_status"))
            .alias("review_status"),
            pl.when(target_mask).then(pl.lit(actor)).otherwise(pl.col("reviewed_by")).alias("reviewed_by"),
            pl.when(target_mask).then(pl.lit(now_utc)).otherwise(pl.col("reviewed_at")).alias("reviewed_at"),
            pl.when(target_mask).then(pl.lit(note)).otherwise(pl.col("review_note")).alias("review_note"),
            pl.when(target_mask & pl.lit(final_contact).is_not_null())
            .then(pl.lit(final_contact))
            .otherwise(pl.col("contact_id"))
            .alias("contact_id"),
        )
        self._write_table("match_results", updated_results)

        event = {
            "event_id": str(uuid.uuid4()),
            "attorney_uuid": attorney_uuid,
            "run_id": effective_run_id,
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
            self.merge_master(effective_run_id)

        return event


class BigQueryStorageBackend:
    """BigQuery storage backend using Load Jobs and parameterized atomic MERGE/UPDATE DML."""

    def __init__(self, project_id: str, dataset_id: str, location: str = "us-central1") -> None:
        from google.auth.exceptions import DefaultCredentialsError
        from google.cloud import bigquery

        self.bigquery = bigquery
        self.project_id = project_id
        self.dataset_id = dataset_id
        self.location = location
        try:
            self.client = self.bigquery.Client(project=project_id, location=location)
        except DefaultCredentialsError as exc:
            raise RuntimeError(
                "BigQuery credentials not found. Run `gcloud auth application-default login` "
                "or configure GOOGLE_APPLICATION_CREDENTIALS."
            ) from exc

    def _table_ref(self, table: str) -> str:
        return f"{self.project_id}.{self.dataset_id}.{table}"

    def _bq_schema_fields(self, table: str) -> list[Any]:
        return [
            self.bigquery.SchemaField(
                name=f["name"],
                field_type=f["type"],
                mode=f.get("mode", "NULLABLE"),
                description=f.get("description", ""),
            )
            for f in bq_schema(table)
        ]

    def ensure_tables(self) -> None:
        """Create the BigQuery dataset and all 6 tables from schemas/*.json if absent."""
        ds = self.bigquery.Dataset(f"{self.project_id}.{self.dataset_id}")
        ds.location = self.location
        self.client.create_dataset(ds, exists_ok=True)

        for tbl_name in TABLES:
            tbl = self.bigquery.Table(self._table_ref(tbl_name), schema=self._bq_schema_fields(tbl_name))
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

    def _load_rows(
        self, table: str, rows: list[dict[str, Any]], write_disposition: str = "WRITE_APPEND"
    ) -> None:
        if not rows:
            return
        cleaned = json.loads(json.dumps(rows, default=str))
        job_config = self.bigquery.LoadJobConfig(
            schema=self._bq_schema_fields(table),
            source_format=self.bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=write_disposition,
        )
        job = self.client.load_table_from_json(cleaned, self._table_ref(table), job_config=job_config)
        job.result()

    def seed_source(self, input_csv_path: Path) -> int:
        self.ensure_tables()
        conformed = prepare_source_dataframe(input_csv_path)
        self._load_rows("attorneys_source", conformed.to_dicts(), write_disposition="WRITE_TRUNCATE")
        return len(conformed)

    def load_source(self) -> pl.DataFrame:
        return self._query_to_df(f"SELECT * FROM `{self._table_ref('attorneys_source')}`", "attorneys_source")

    def save_run(
        self,
        run_record: dict[str, Any],
        candidates: list[dict[str, Any]],
        results: list[dict[str, Any]],
    ) -> None:
        self.ensure_tables()
        if candidates:
            self._load_rows(
                "match_candidates", conform(pl.DataFrame(candidates), "match_candidates").to_dicts()
            )
        if results:
            self._load_rows("match_results", conform(pl.DataFrame(results), "match_results").to_dicts())
        self._load_rows("pipeline_runs", conform(pl.DataFrame([run_record]), "pipeline_runs").to_dicts())

    def merge_master(self, run_id: str) -> int:
        query = f"""
        MERGE `{self._table_ref("attorneys_master")}` T
        USING (
            SELECT * FROM `{self._table_ref("match_results")}`
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
        except Exception as exc:
            logger.debug("Unable to fetch latest run_id: %s", exc)
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
        return self._query_to_df(
            q, "match_results", job_config=self.bigquery.QueryJobConfig(query_parameters=params)
        )

    def get_candidates(self, attorney_uuid: str, run_id: str | None = None) -> pl.DataFrame:
        target_run_id = run_id or self.get_latest_run_id()
        q = f"SELECT * FROM `{self._table_ref('match_candidates')}` WHERE attorney_uuid = @attorney_uuid"
        params = [self.bigquery.ScalarQueryParameter("attorney_uuid", "STRING", attorney_uuid)]
        if target_run_id and target_run_id != "all":
            q += " AND run_id = @run_id"
            params.append(self.bigquery.ScalarQueryParameter("run_id", "STRING", target_run_id))
        return self._query_to_df(
            q, "match_candidates", job_config=self.bigquery.QueryJobConfig(query_parameters=params)
        )

    def apply_human_review(
        self,
        attorney_uuid: str,
        action: str,
        chosen_contact_id: str | None,
        actor: str,
        note: str = "",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        target_run_id = run_id or self.get_latest_run_id()
        if not target_run_id:
            raise KeyError("No pipeline runs found to apply human review.")

        lookup_query = f"""
        SELECT contact_id, review_status
        FROM `{self._table_ref("match_results")}`
        WHERE attorney_uuid = @attorney_uuid AND run_id = @run_id
        LIMIT 1
        """
        lookup_cfg = self.bigquery.QueryJobConfig(
            query_parameters=[
                self.bigquery.ScalarQueryParameter("attorney_uuid", "STRING", attorney_uuid),
                self.bigquery.ScalarQueryParameter("run_id", "STRING", target_run_id),
            ]
        )
        existing = list(self.client.query(lookup_query, job_config=lookup_cfg).result())
        if not existing:
            raise KeyError(f"Attorney {attorney_uuid} not found in run {target_run_id}.")

        prev_status = existing[0]["review_status"]
        prev_contact = existing[0]["contact_id"]

        now_utc = datetime.now(UTC)
        new_status = "human_approved" if action in ("approve", "select_candidate") else "human_rejected"
        final_contact = chosen_contact_id if chosen_contact_id else prev_contact

        update_query = f"""
        UPDATE `{self._table_ref("match_results")}`
        SET review_status = @new_status,
            reviewed_by = @actor,
            reviewed_at = @reviewed_at,
            review_note = @note,
            contact_id = COALESCE(@chosen_contact_id, contact_id)
        WHERE attorney_uuid = @attorney_uuid AND run_id = @run_id;
        """
        job_config = self.bigquery.QueryJobConfig(
            query_parameters=[
                self.bigquery.ScalarQueryParameter("new_status", "STRING", new_status),
                self.bigquery.ScalarQueryParameter("actor", "STRING", actor),
                self.bigquery.ScalarQueryParameter("reviewed_at", "TIMESTAMP", now_utc),
                self.bigquery.ScalarQueryParameter("note", "STRING", note),
                self.bigquery.ScalarQueryParameter("chosen_contact_id", "STRING", final_contact),
                self.bigquery.ScalarQueryParameter("attorney_uuid", "STRING", attorney_uuid),
                self.bigquery.ScalarQueryParameter("run_id", "STRING", target_run_id),
            ]
        )
        self.client.query(update_query, job_config=job_config).result()

        event = {
            "event_id": str(uuid.uuid4()),
            "attorney_uuid": attorney_uuid,
            "run_id": target_run_id,
            "action": action,
            "previous_status": prev_status,
            "new_status": new_status,
            "previous_contact_id": prev_contact,
            "new_contact_id": final_contact,
            "actor": actor,
            "note": note,
            "created_at": now_utc,
        }
        self._load_rows("review_events", conform(pl.DataFrame([event]), "review_events").to_dicts())

        if new_status == "human_approved":
            if final_contact:
                cache_query = f"""
                UPDATE `{self._table_ref("attorneys_source")}`
                SET txbar_contact_id = @contact_id, last_verified_at = @verified_at
                WHERE attorney_uuid = @attorney_uuid;
                """
                cache_cfg = self.bigquery.QueryJobConfig(
                    query_parameters=[
                        self.bigquery.ScalarQueryParameter("contact_id", "STRING", final_contact),
                        self.bigquery.ScalarQueryParameter("verified_at", "TIMESTAMP", now_utc),
                        self.bigquery.ScalarQueryParameter("attorney_uuid", "STRING", attorney_uuid),
                    ]
                )
                self.client.query(cache_query, job_config=cache_cfg).result()
            self.merge_master(target_run_id)

        return event


def get_storage(settings: Settings | None = None) -> StorageBackend:
    s = settings or get_settings()
    if s.google_application_credentials:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = s.google_application_credentials
    if s.storage_backend == "bigquery":
        if not s.gcp_project_id:
            raise ValueError("GCP_PROJECT_ID is required when STORAGE_BACKEND=bigquery.")
        return BigQueryStorageBackend(
            project_id=s.gcp_project_id,
            dataset_id=s.bq_dataset,
            location=s.gcp_region,
        )
    return CsvStorageBackend(warehouse_dir=s.resolve(s.local_warehouse_dir))
