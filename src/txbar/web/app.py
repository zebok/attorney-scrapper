"""FastAPI application for the Texas Bar Human-in-the-Loop Review Console."""

from __future__ import annotations

import hmac
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import polars as pl
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from txbar.client import TexasBarClient
from txbar.config import Settings, get_settings
from txbar.pipeline import format_delivery_dataframe
from txbar.storage import StorageBackend, get_storage

app = FastAPI(
    title="Texas Bar Attorney Enrichment & Review API",
    description="Automated enrichment pipeline with human-in-the-loop review queue.",
    version="0.1.0",
)


def get_current_storage(settings: Settings = Depends(get_settings)) -> StorageBackend:
    return get_storage(settings)


def verify_reviewer_token(
    x_reviewer_token: str | None = Header(None),
    settings: Settings = Depends(get_settings),
) -> str:
    """Validate X-Reviewer-Token whenever REVIEWER_TOKEN is configured."""
    expected = settings.reviewer_token.strip()
    if expected:
        provided = (x_reviewer_token or "").strip()
        if not provided or not hmac.compare_digest(provided, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing X-Reviewer-Token header",
            )
        return "verified_reviewer"
    return "local_reviewer"


class ReviewActionRequest(BaseModel):
    action: Literal["approve", "reject", "select_candidate"]
    chosen_contact_id: str | None = None
    note: str = Field(default="", max_length=500)
    reviewer_name: str = Field(default="", max_length=100)


@app.get("/health")
def health_endpoint(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    with TexasBarClient(settings) as client:
        return asdict(client.health_check())


@app.get("/api/v1/stats")
def get_stats(
    run_id: str | None = Query(None),
    storage: StorageBackend = Depends(get_current_storage),
) -> dict[str, Any]:
    results = storage.get_all_results(run_id=run_id)
    total = len(results)
    if total == 0:
        return {
            "total": 0,
            "auto_approved": 0,
            "pending_review": 0,
            "human_approved": 0,
            "human_rejected": 0,
            "not_found": 0,
            "firm_changes": 0,
            "phones_found": 0,
        }
    phones_found = int(results["phone"].is_not_null().sum()) if "phone" in results.columns else 0
    return {
        "total": total,
        "auto_approved": int((results["review_status"] == "auto_approved").sum()),
        "pending_review": int((results["review_status"] == "pending_review").sum()),
        "human_approved": int((results["review_status"] == "human_approved").sum()),
        "human_rejected": int((results["review_status"] == "human_rejected").sum()),
        "not_found": int((results["review_status"] == "not_found").sum()),
        "firm_changes": int(results["firm_changed"].fill_null(False).sum()),
        "phones_found": phones_found,
    }


@app.get("/api/v1/export-csv")
def export_csv(
    run_id: str | None = Query(None),
    storage: StorageBackend = Depends(get_current_storage),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Download the enriched Texas Bar CSV deliverable from active storage."""
    df = storage.get_all_results(run_id=run_id)
    if len(df) > 0:
        csv_content = format_delivery_dataframe(df).write_csv()
        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="enriched_tx_lawyers.csv"'},
        )

    csv_path = settings.resolve(settings.output_dir / "enriched_tx_lawyers.csv")
    if not csv_path.exists():
        raise HTTPException(status_code=404, detail="No enriched records found in storage.")
    return FileResponse(path=csv_path, media_type="text/csv", filename="enriched_tx_lawyers.csv")


@app.get("/api/v1/queue")
def get_queue(
    run_id: str | None = Query(None),
    storage: StorageBackend = Depends(get_current_storage),
) -> list[dict[str, Any]]:
    return storage.get_review_queue(run_id=run_id).to_dicts()


@app.get("/api/v1/results")
def get_results(
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = Query(None),
    run_id: str | None = Query(None),
    storage: StorageBackend = Depends(get_current_storage),
) -> list[dict[str, Any]]:
    df = storage.get_all_results(run_id=run_id)
    if status_filter:
        df = df.filter(pl.col("review_status") == status_filter)
    if search and search.strip():
        needle = search.strip().lower()
        search_cols = [
            c
            for c in (
                "input_name",
                "input_firm",
                "input_city",
                "full_name",
                "current_firm",
                "bar_card_number",
            )
            if c in df.columns
        ]
        if search_cols:
            expr = pl.any_horizontal(
                pl.col(c).cast(pl.Utf8).fill_null("").str.to_lowercase().str.contains(needle, literal=True)
                for c in search_cols
            )
            df = df.filter(expr)
    return df.to_dicts()


@app.get("/api/v1/candidates/{attorney_uuid}")
def get_candidates(
    attorney_uuid: str,
    run_id: str | None = Query(None),
    storage: StorageBackend = Depends(get_current_storage),
) -> list[dict[str, Any]]:
    return storage.get_candidates(attorney_uuid, run_id=run_id).to_dicts()


@app.patch("/api/v1/review/{attorney_uuid}")
def patch_review(
    attorney_uuid: str,
    payload: ReviewActionRequest,
    run_id: str | None = Query(None),
    storage: StorageBackend = Depends(get_current_storage),
    auth_actor: str = Depends(verify_reviewer_token),
) -> dict[str, Any]:
    try:
        actor = payload.reviewer_name.strip() or auth_actor
        event = storage.apply_human_review(
            attorney_uuid=attorney_uuid,
            action=payload.action,
            chosen_contact_id=payload.chosen_contact_id,
            actor=actor,
            note=payload.note,
            run_id=run_id,
        )
        return {"status": "ok", "event": event}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/", response_class=HTMLResponse)
def index_page() -> str:
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>Review UI template missing</h1>"
