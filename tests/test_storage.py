"""Unit tests for CsvStorageBackend run scoping, master merges, and factory guards."""

from datetime import UTC, datetime

import pytest

from txbar.config import Settings
from txbar.storage import CsvStorageBackend, get_storage


def _seed_two_runs(storage: CsvStorageBackend) -> str:
    attorney_uuid = "11111111-1111-1111-1111-111111111111"
    storage.save_run(
        run_record={
            "run_id": "run-1",
            "started_at": datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            "status": "succeeded",
        },
        candidates=[
            {
                "run_id": "run-1",
                "attorney_uuid": attorney_uuid,
                "contact_id": "c-old",
                "search_stage": "stage1",
                "rank": 1,
                "score": 70.0,
                "full_name": "Jane Doe (Old)",
            }
        ],
        results=[
            {
                "attorney_uuid": attorney_uuid,
                "run_id": "run-1",
                "input_name": "Jane Doe",
                "contact_id": "c-old",
                "full_name": "Jane Doe (Old)",
                "match_score": 70.0,
                "match_status": "low_confidence",
                "review_status": "pending_review",
            }
        ],
    )
    storage.save_run(
        run_record={
            "run_id": "run-2",
            "started_at": datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
            "status": "succeeded",
        },
        candidates=[
            {
                "run_id": "run-2",
                "attorney_uuid": attorney_uuid,
                "contact_id": "c-new",
                "search_stage": "stage1",
                "rank": 1,
                "score": 92.0,
                "full_name": "Jane Doe",
            }
        ],
        results=[
            {
                "attorney_uuid": attorney_uuid,
                "run_id": "run-2",
                "input_name": "Jane Doe",
                "contact_id": "c-new",
                "full_name": "Jane Doe",
                "match_score": 92.0,
                "match_status": "exact_match",
                "review_status": "auto_approved",
            }
        ],
    )
    return attorney_uuid


def test_csv_storage_scopes_candidates_and_reviews_by_run_id(tmp_path):
    storage = CsvStorageBackend(warehouse_dir=tmp_path / "warehouse")
    attorney_uuid = _seed_two_runs(storage)

    latest_cands = storage.get_candidates(attorney_uuid)
    assert latest_cands["contact_id"].to_list() == ["c-new"]

    run1_cands = storage.get_candidates(attorney_uuid, run_id="run-1")
    assert run1_cands["contact_id"].to_list() == ["c-old"]

    storage.apply_human_review(
        attorney_uuid=attorney_uuid,
        action="approve",
        chosen_contact_id="c-old",
        actor="reviewer@test",
        note="Verified old run",
        run_id="run-1",
    )

    run1_res = storage.get_all_results(run_id="run-1").to_dicts()[0]
    run2_res = storage.get_all_results(run_id="run-2").to_dicts()[0]
    assert run1_res["review_status"] == "human_approved"
    assert run2_res["review_status"] == "auto_approved"


def test_csv_merge_master_conforms_schema(tmp_path):
    storage = CsvStorageBackend(warehouse_dir=tmp_path / "warehouse")
    _seed_two_runs(storage)

    merged = storage.merge_master("run-2")
    assert merged == 1
    master = storage._read_table("attorneys_master")
    assert len(master) == 1
    assert master["contact_id"].to_list() == ["c-new"]


def test_get_storage_bigquery_requires_project_id():
    settings = Settings(storage_backend="bigquery", gcp_project_id="")
    with pytest.raises(ValueError, match="GCP_PROJECT_ID"):
        get_storage(settings)
