"""FastAPI application for the Texas Bar Human-in-the-Loop Review Dashboard and API."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from txbar.client import TexasBarClient
from txbar.config import Settings, get_settings
from txbar.storage import StorageBackend, get_storage

app = FastAPI(
    title="Texas Bar Attorney Enrichment & Review API",
    description="Automated enrichment pipeline with human-in-the-loop review queue.",
    version="0.1.0",
)


def get_current_storage() -> StorageBackend:
    return get_storage(get_settings())


def verify_reviewer_token(
    x_reviewer_token: str | None = Header(None),
    settings: Settings = Depends(get_settings),
) -> str:
    """Validate token for mutating operations (PATCH)."""
    expected = settings.reviewer_token
    # If set to default or token provided matches, allow.
    if expected and expected != "change-me" and x_reviewer_token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Reviewer-Token header",
        )
    return x_reviewer_token or "anonymous_reviewer"


class ReviewActionRequest(BaseModel):
    action: Literal["approve", "reject", "select_candidate"]
    chosen_contact_id: str | None = None
    note: str = Field(default="", max_length=500)
    reviewer_name: str = Field(default="reviewer", max_length=100)


class LiveLookupRequest(BaseModel):
    name: str
    firm: str = ""
    city: str = ""
    jd_year: int | None = None
    email: str = ""


class TriggerRunRequest(BaseModel):
    limit: int | None = None


_pipeline_state: dict[str, Any] = {
    "is_running": False,
    "run_id": None,
    "last_result": None,
}


@app.get("/health")
def health_endpoint() -> dict[str, Any]:
    with TexasBarClient(get_settings()) as client:
        return client.health_check()


@app.get("/api/v1/stats")
def get_stats(storage: StorageBackend = Depends(get_current_storage)) -> dict[str, Any]:
    results = storage.get_all_results()
    total = len(results)
    if total == 0:
        return {"total": 0, "auto_approved": 0, "pending_review": 0, "not_found": 0, "firm_changes": 0}
    return {
        "total": total,
        "auto_approved": int((results["review_status"] == "auto_approved").sum()),
        "pending_review": int((results["review_status"] == "pending_review").sum()),
        "human_approved": int((results["review_status"] == "human_approved").sum()),
        "human_rejected": int((results["review_status"] == "human_rejected").sum()),
        "not_found": int((results["review_status"] == "not_found").sum()),
        "firm_changes": int((results["firm_changed"] == True).sum()),  # noqa: E712
    }


@app.get("/api/v1/queue")
def get_queue(storage: StorageBackend = Depends(get_current_storage)) -> list[dict[str, Any]]:
    queue_df = storage.get_review_queue()
    return queue_df.to_dicts()


@app.get("/api/v1/results")
def get_results(
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = Query(None),
    storage: StorageBackend = Depends(get_current_storage),
) -> list[dict[str, Any]]:
    df = storage.get_all_results()
    if status_filter:
        df = df.filter(df["review_status"] == status_filter)
    if search:
        s_lower = search.strip().lower()
        df = df.filter(
            df["input_name"].str.to_lowercase().str.contains(s_lower)
            | df["input_firm"].str.to_lowercase().str.contains(s_lower)
            | df["full_name"].str.to_lowercase().str.contains(s_lower)
        )
    return df.to_dicts()


@app.get("/api/v1/candidates/{attorney_uuid}")
def get_candidates(
    attorney_uuid: str, storage: StorageBackend = Depends(get_current_storage)
) -> list[dict[str, Any]]:
    cand_df = storage.get_candidates(attorney_uuid)
    return cand_df.to_dicts()


@app.patch("/api/v1/review/{attorney_uuid}")
def patch_review(
    attorney_uuid: str,
    payload: ReviewActionRequest,
    storage: StorageBackend = Depends(get_current_storage),
    actor: str = Depends(verify_reviewer_token),
) -> dict[str, Any]:
    try:
        final_actor = payload.reviewer_name or actor
        event = storage.apply_human_review(
            attorney_uuid=attorney_uuid,
            action=payload.action,
            chosen_contact_id=payload.chosen_contact_id,
            actor=final_actor,
            note=payload.note,
        )
        return {"status": "ok", "event": event}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/lookup-live")
def post_lookup_live(payload: LiveLookupRequest) -> dict[str, Any]:
    """Execute on-demand live lookup against the Texas Bar directory."""
    from txbar.matching import MatchEngine

    with TexasBarClient(get_settings()) as client:
        engine = MatchEngine(client, get_settings())
        decision = engine.resolve_attorney(payload.model_dump())
        best = decision.best_candidate
        prof = best.profile if best else None

        return {
            "query": payload.model_dump(),
            "match_score": decision.match_score,
            "match_status": decision.match_status,
            "match_explanation": decision.match_explanation,
            "firm_changed": decision.firm_changed,
            "search_stage": decision.search_stage,
            "best_candidate": {
                "contact_id": best.contact_id if best else None,
                "full_name": prof.name.full_name if prof else (best.full_name if best else None),
                "preferred_name": prof.name.preferred_name
                if prof
                else (best.preferred_name if best else None),
                "bar_card_number": prof.bar_card_number if prof else (best.bar_card_number if best else None),
                "license_status": prof.license_status if prof else (best.license_status if best else None),
                "license_date": prof.license_date.isoformat() if (prof and prof.license_date) else None,
                "current_firm": prof.firm if prof else (best.firm if best else None),
                "city": prof.city if prof else (best.city if best else None),
                "phone": prof.phone if prof else None,
                "address": prof.address if prof else None,
                "law_school": prof.law_school if prof else None,
                "grad_year": prof.grad_year if prof else None,
                "practice_areas": prof.practice_areas if prof else [],
            }
            if best
            else None,
            "candidates": [
                {
                    "contact_id": c.contact_id,
                    "full_name": c.full_name,
                    "firm": c.firm,
                    "city": c.city,
                    "score": c.score,
                    "explanation": c.explanation,
                }
                for c in decision.candidates
            ],
        }


@app.post("/api/v1/trigger-run")
def post_trigger_run(
    payload: TriggerRunRequest,
    actor: str = Depends(verify_reviewer_token),
) -> dict[str, Any]:
    """Trigger the enrichment pipeline asynchronously in the background."""
    import threading

    global _pipeline_state
    if _pipeline_state["is_running"]:
        return {"status": "already_running", "run_id": _pipeline_state["run_id"]}

    from txbar.pipeline import Pipeline

    def _worker(lim: int | None):
        global _pipeline_state
        _pipeline_state["is_running"] = True
        try:
            p = Pipeline(get_settings())
            res = p.run(limit=lim)
            _pipeline_state["last_result"] = res
            _pipeline_state["run_id"] = res.get("run_id")
        finally:
            _pipeline_state["is_running"] = False

    t = threading.Thread(target=_worker, args=(payload.limit,), daemon=True)
    t.start()
    return {"status": "started", "limit": payload.limit, "actor": actor}


@app.get("/api/v1/run-status")
def get_run_status() -> dict[str, Any]:
    global _pipeline_state
    return _pipeline_state


@app.get("/", response_class=HTMLResponse)
def index_page() -> str:
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>Review UI template missing</h1>"
