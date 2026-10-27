"""Test Pipeline using Mocked DirectoryProvider (Dependency Injection test)."""

from datetime import date

from txbar.pipeline import Pipeline
from txbar.provider import DirectoryHit, ProviderHealth
from txbar.storage import CsvStorageBackend


class MockProvider:
    provider_id = "mock_bar"
    provider_name = "Mock Bar Association"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def search_by_affiliation(self, last_name: str, firm_core: str):
        return [
            DirectoryHit(
                external_id="mock_101",
                first_name="Jane",
                last_name="Doe",
                full_name="Jane Doe",
                firm="Test Law LLP",
                city="Austin",
                license_status="Eligible to Practice",
            )
        ]

    def search_by_location(self, name: str, city: str):
        return []

    def search_statewide(self, last_name: str):
        return []

    def get_profile(self, external_id: str):
        from txbar.parser import NameParts, Profile

        return Profile(
            contact_id="mock_101",
            name=NameParts(first_name="Jane", last_name="Doe"),
            license_status="Eligible to Practice in Texas",
            bar_card_number="12345678",
            license_date=date(2018, 11, 2),
            firm="Test Law LLP",
            city="Austin",
            phone="+15125551234",
            grad_year=2018,
        )

    def health_check(self):
        return {"status": "healthy", "latency_s": 0.01, "canary_passed": True}

    def check_health(self):
        return ProviderHealth(status="healthy", latency_s=0.01, canary_passed=True)


def test_pipeline_with_mock_provider(tmp_path):
    storage = CsvStorageBackend(warehouse_dir=tmp_path / "warehouse")

    # Seed 1 attorney into source
    import polars as pl

    src_df = pl.DataFrame(
        [
            {
                "attorney_uuid": "00000000-0000-0000-0000-000000000001",
                "name": "Jane Doe",
                "firm": "Test Law LLP",
                "title": "Partner",
                "practice": "Corporate",
                "city": "Austin",
                "state": "Texas",
                "jd_year": 2018,
                "email": "jdoe@testlaw.com",
                "txbar_contact_id": None,
                "bar_card_number": None,
                "last_verified_at": None,
                "ingested_at": "2026-10-27T00:00:00Z",
            }
        ]
    )
    storage._write_table("attorneys_source", src_df)

    # Inject mock provider into Pipeline with isolated output directory!
    from txbar.config import Settings

    settings = Settings(output_dir=tmp_path / "output")
    pipeline = Pipeline(settings=settings, storage=storage, provider=MockProvider())  # type: ignore
    result = pipeline.run(limit=1, seed_if_empty=False)

    assert result["status"] == "succeeded"
    assert result["total"] == 1
    assert result["auto_approved"] == 1
    assert result["dq_gate_passed"] is True
