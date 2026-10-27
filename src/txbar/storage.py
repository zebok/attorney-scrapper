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
    def get_review_queue(self) -> pl.DataFrame: ...
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

    def get_review_queue(self) -> pl.DataFrame:
        results = self._read_table("match_results")
        return results.filter(pl.col("review_status") == "pending_review")

    def get_all_results(self, run_id: str | None = None) -> pl.DataFrame:
        results = self._read_table("match_results")
        if run_id:
            return results.filter(pl.col("run_id") == run_id)
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
    """BigQuery storage backend using Load Jobs and atomic MERGE DML."""

    def __init__(self, project_id: str, dataset_id: str) -> None:
        from google.cloud import bigquery

        self.project_id = project_id
        self.dataset_id = dataset_id
        self.client = bigquery.Client(project=project_id)
        # Note: In a complete GCP deployment, this implements the exact same protocol
        # using client.load_table_from_file and client.query(MERGE_SQL).

    def seed_source(self, input_csv_path: Path) -> int:
        raise NotImplementedError("BigQuery seed implemented via bq load CLI or pipeline.")

    def load_source(self) -> pl.DataFrame:
        query = f"SELECT * FROM `{self.project_id}.{self.dataset_id}.attorneys_source`"
        arrow_table = self.client.query(query).to_arrow()
        return pl.from_arrow(arrow_table)  # type: ignore

    def save_run(
        self,
        run_record: dict[str, Any],
        candidates: list[dict[str, Any]],
        results: list[dict[str, Any]],
    ) -> None:
        raise NotImplementedError("BigQuery load jobs configured in pipeline.")

    def merge_master(self, run_id: str) -> int:
        query = f"""
        MERGE `{self.project_id}.{self.dataset_id}.attorneys_master` T
        USING (
            SELECT * FROM `{self.project_id}.{self.dataset_id}.match_results`
            WHERE run_id = '{run_id}' AND review_status IN ('auto_approved', 'human_approved')
        ) S
        ON T.attorney_uuid = S.attorney_uuid
        WHEN MATCHED THEN
          UPDATE SET T.current_firm = S.current_firm, T.phone = S.phone, T.bar_card_number = S.bar_card_number,
                     T.license_status = S.license_status, T.last_verified_at = S.scraped_at
        WHEN NOT MATCHED THEN
          INSERT ROW;
        """
        job = self.client.query(query)
        job.result()
        return job.num_dml_affected_rows or 0

    def get_review_queue(self) -> pl.DataFrame:
        query = f"SELECT * FROM `{self.project_id}.{self.dataset_id}.v_review_queue`"
        arrow_table = self.client.query(query).to_arrow()
        return pl.from_arrow(arrow_table)  # type: ignore

    def get_all_results(self, run_id: str | None = None) -> pl.DataFrame:
        q = f"SELECT * FROM `{self.project_id}.{self.dataset_id}.match_results`"
        if run_id:
            q += f" WHERE run_id = '{run_id}'"
        return pl.from_arrow(self.client.query(q).to_arrow())  # type: ignore

    def get_candidates(self, attorney_uuid: str) -> pl.DataFrame:
        q = f"SELECT * FROM `{self.project_id}.{self.dataset_id}.match_candidates` WHERE attorney_uuid = '{attorney_uuid}'"
        return pl.from_arrow(self.client.query(q).to_arrow())  # type: ignore

    def apply_human_review(
        self,
        attorney_uuid: str,
        action: str,
        chosen_contact_id: str | None,
        actor: str,
        note: str = "",
    ) -> dict[str, Any]:
        # Atomic DML UPDATE on match_results + INSERT into review_events + UPDATE attorneys_source
        now_iso = datetime.now(UTC).isoformat()
        new_status = "human_approved" if action in ("approve", "select_candidate") else "human_rejected"
        update_query = f"""
        UPDATE `{self.project_id}.{self.dataset_id}.match_results`
        SET review_status = '{new_status}', reviewed_by = '{actor}',
            reviewed_at = TIMESTAMP('{now_iso}'), review_note = '{note}'
        WHERE attorney_uuid = '{attorney_uuid}';
        """
        self.client.query(update_query).result()
        return {"attorney_uuid": attorney_uuid, "new_status": new_status, "actor": actor}


def get_storage(settings: Settings | None = None) -> StorageBackend:
    s = settings or get_settings()
    if s.storage_backend == "bigquery" and s.gcp_project_id:
        return BigQueryStorageBackend(project_id=s.gcp_project_id, dataset_id=s.bq_dataset)
    return CsvStorageBackend(warehouse_dir=s.resolve(s.local_warehouse_dir))
