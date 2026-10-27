"""Single source of truth for table schemas.

The JSON files under /schemas are consumed verbatim by Terraform (google_bigquery_table.schema)
and by this module to type Polars frames, so the CSV backend mirrors BigQuery 1:1.
"""

from __future__ import annotations

import json
from functools import lru_cache

import polars as pl

from txbar.config import PROJECT_ROOT

SCHEMA_DIR = PROJECT_ROOT / "schemas"

TABLES = ("attorneys_source", "match_candidates", "match_results", "review_events", "pipeline_runs")

_BQ_TO_POLARS: dict[str, pl.DataType] = {
    "STRING": pl.Utf8,
    "INT64": pl.Int64,
    "FLOAT64": pl.Float64,
    "BOOL": pl.Boolean,
    "DATE": pl.Date,
    "TIMESTAMP": pl.Datetime("us", "UTC"),
}


@lru_cache
def bq_schema(table: str) -> list[dict]:
    return json.loads((SCHEMA_DIR / f"{table}.json").read_text())


@lru_cache
def polars_schema(table: str) -> dict[str, pl.DataType]:
    return {f["name"]: _BQ_TO_POLARS[f["type"]] for f in bq_schema(table)}


def columns(table: str) -> list[str]:
    return [f["name"] for f in bq_schema(table)]


def required_columns(table: str) -> list[str]:
    return [f["name"] for f in bq_schema(table) if f.get("mode") == "REQUIRED"]


def conform(df: pl.DataFrame, table: str) -> pl.DataFrame:
    """Add missing columns as nulls, drop unknown ones, cast to the declared types, keep schema order."""
    schema = polars_schema(table)
    exprs = []
    for name, dtype in schema.items():
        if name in df.columns:
            col = pl.col(name)
            src = df.schema[name]
            if dtype == pl.Date and src == pl.Utf8:
                col = col.str.to_date(strict=False)
            elif isinstance(dtype, pl.Datetime) and src == pl.Utf8:
                col = col.str.to_datetime(strict=False, time_zone="UTC")
            elif isinstance(dtype, pl.Datetime) and isinstance(src, pl.Datetime) and src.time_zone is None:
                col = col.dt.replace_time_zone("UTC")
            exprs.append(col.cast(dtype, strict=False).alias(name))
        else:
            exprs.append(pl.lit(None, dtype=dtype).alias(name))
    return df.select(exprs)


def empty(table: str) -> pl.DataFrame:
    return pl.DataFrame(schema=polars_schema(table))
