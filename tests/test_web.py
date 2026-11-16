"""API tests for the FastAPI review console."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from txbar.config import Settings, get_settings
from txbar.storage import CsvStorageBackend
from txbar.web.app import app, get_current_storage


def _build_test_storage(tmp_path) -> tuple[CsvStorageBackend, str]:
    storage = CsvStorageBackend(warehouse_dir=tmp_path / "warehouse")
    attorney_uuid = "22222222-2222-2222-2222-222222222222"
    storage.save_run(
        run_record={
            "run_id": "run-ui-1",
            "started_at": datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
            "status": "succeeded",
        },
        candidates=[
            {
                "run_id": "run-ui-1",
                "attorney_uuid": attorney_uuid,
                "contact_id": "354809",
                "search_stage": "stage1",
                "rank": 1,
                "score": 78.0,
                "full_name": "Michael Roberts",
                "firm": "Sidley Austin, LLP",
            }
        ],
        results=[
            {
                "attorney_uuid": attorney_uuid,
                "run_id": "run-ui-1",
                "input_name": "Michael Roberts",
                "input_firm": "Sidley Austin (LLP)",
                "input_city": None,
                "contact_id": "354809",
                "full_name": "Michael Roberts",
                "current_firm": "Sidley Austin, LLP",
                "bar_card_number": "24098330",
                "phone": "+12145550100",
                "match_score": 78.0,
                "match_status": "low_confidence",
                "review_status": "pending_review",
                "firm_changed": False,
            }
        ],
    )
    return storage, attorney_uuid


def test_review_api_endpoints_and_search_literal(tmp_path):
    storage, attorney_uuid = _build_test_storage(tmp_path)
    settings = Settings(storage_backend="csv", reviewer_token="", output_dir=tmp_path / "out")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_storage] = lambda: storage
    try:
        client = TestClient(app)

        html = client.get("/")
        assert html.status_code == 200
        assert 'id="health-text"' in html.text

        stats = client.get("/api/v1/stats")
        assert stats.status_code == 200
        assert stats.json()["pending_review"] == 1
        assert stats.json()["phones_found"] == 1

        # Regex characters like '(' must be treated literally without crashing on nulls.
        res = client.get("/api/v1/results", params={"search": "(LLP)"})
        assert res.status_code == 200
        assert len(res.json()) == 1

        csv_resp = client.get("/api/v1/export-csv")
        assert csv_resp.status_code == 200
        assert "Michael Roberts" in csv_resp.text

        # Without configured reviewer_token, PATCH works in local mode.
        patch_resp = client.patch(
            f"/api/v1/review/{attorney_uuid}",
            json={"action": "approve", "chosen_contact_id": "354809", "note": "ok"},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["event"]["new_status"] == "human_approved"
    finally:
        app.dependency_overrides.clear()


def test_review_patch_enforces_reviewer_token(tmp_path):
    storage, attorney_uuid = _build_test_storage(tmp_path)
    settings = Settings(storage_backend="csv", reviewer_token="super-secret-token")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_storage] = lambda: storage
    try:
        client = TestClient(app)
        payload = {"action": "reject", "note": "wrong person"}

        assert client.patch(f"/api/v1/review/{attorney_uuid}", json=payload).status_code == 401
        assert (
            client.patch(
                f"/api/v1/review/{attorney_uuid}",
                json=payload,
                headers={"X-Reviewer-Token": "wrong"},
            ).status_code
            == 401
        )
        ok_resp = client.patch(
            f"/api/v1/review/{attorney_uuid}",
            json=payload,
            headers={"X-Reviewer-Token": "super-secret-token"},
        )
        assert ok_resp.status_code == 200
        assert ok_resp.json()["event"]["new_status"] == "human_rejected"
    finally:
        app.dependency_overrides.clear()
