"""Unit tests for entity resolution and match confidence scoring."""

from datetime import date

from txbar.matching import MatchEngine
from txbar.normalize import parse_email, parse_input_name
from txbar.provider import NameParts, Profile, ProviderHealth, SearchHit
from txbar.signals import ScoringConfig


def test_score_exact_match():
    engine = MatchEngine()

    input_rec = {
        "name": "W. Buechele",
        "firm": "Winstead PC",
        "city": "Dallas",
        "jd_year": 2016,
        "email": "bbuechele@winstead.com",
    }
    parsed_name = parse_input_name(input_rec["name"], input_rec["email"])
    email_hints = parse_email(input_rec["email"], parsed_name)

    hit = SearchHit(
        contact_id="350258",
        name=NameParts(
            first_name="William", middle_name="Bryant", last_name="Buechele", preferred_name="Bryant Buechele"
        ),
        firm="Winstead PC",
        city="Dallas",
        license_status="Eligible to Practice in Texas",
    )
    prof = Profile(
        contact_id="350258",
        name=hit.name,
        license_status="Eligible to Practice in Texas",
        bar_card_number="24101572",
        license_date=date(2016, 11, 4),
        firm="Winstead PC",
        city="Dallas",
        grad_year=2016,
    )

    eval_res = engine.evaluate_candidate(hit, input_rec, parsed_name, email_hints, prof, stage="test", rank=1)

    assert eval_res.score >= 85.0
    assert eval_res.score_name == 35.0  # matched preferred / middle name
    assert eval_res.score_firm == 25.0  # exact firm match
    assert eval_res.score_temporal >= 22.0
    assert eval_res.score_geo == 15.0


def test_homonym_disambiguation_with_email_middle_initial():
    engine = MatchEngine()

    # Michael Roberts with mlroberts@sidley.com
    input_rec = {
        "name": "Michael Roberts",
        "firm": "Sidley Austin LLP",
        "city": "Dallas",
        "jd_year": 2017,
        "email": "mlroberts@sidley.com",
    }
    parsed_name = parse_input_name(input_rec["name"], input_rec["email"])
    email_hints = parse_email(input_rec["email"], parsed_name)

    # Candidate A: Michael Lee Roberts (Roberts Law Firm)
    hit_a = SearchHit(
        contact_id="288529",
        name=NameParts(first_name="Michael", middle_name="Lee", last_name="Roberts"),
        firm="Roberts Law Firm",
        city="Dallas",
    )
    prof_a = Profile(
        contact_id="288529", name=hit_a.name, firm="Roberts Law Firm", city="Dallas", grad_year=1990
    )
    cand_a = engine.evaluate_candidate(
        hit_a, input_rec, parsed_name, email_hints, prof_a, stage="test", rank=1
    )

    # Candidate B: Michael L. Roberts (Sidley Austin, LLP)
    hit_b = SearchHit(
        contact_id="354809",
        name=NameParts(first_name="Michael", middle_name="L.", last_name="Roberts"),
        firm="Sidley Austin, LLP",
        city="Dallas",
    )
    prof_b = Profile(
        contact_id="354809", name=hit_b.name, firm="Sidley Austin, LLP", city="Dallas", grad_year=2017
    )
    cand_b = engine.evaluate_candidate(
        hit_b, input_rec, parsed_name, email_hints, prof_b, stage="test", rank=2
    )

    assert cand_b.score > cand_a.score
    assert cand_b.score >= 85.0
    assert cand_b.score_firm == 25.0


class _CachedMoveProvider:
    provider_id = "stub"
    provider_name = "Stub Bar"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def profile_url(self, contact_id: str) -> str:
        return f"https://example.com/{contact_id}"

    def search_by_affiliation(self, last_name: str, firm_core: str):
        raise AssertionError("Stage 0 cache lookup should bypass search")

    def search_by_location(self, name: str, city: str):
        raise AssertionError("Stage 0 cache lookup should bypass search")

    def search_statewide(self, last_name: str):
        raise AssertionError("Stage 0 cache lookup should bypass search")

    def get_profile(self, contact_id: str) -> Profile:
        return Profile(
            contact_id=contact_id,
            name=NameParts(first_name="William", middle_name="Bryant", last_name="Buechele"),
            license_status="Eligible to Practice in Texas",
            bar_card_number="24101572",
            license_date=date(2016, 11, 4),
            firm="Baker Botts LLP",
            city="Dallas",
            grad_year=2016,
        )

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(status="healthy", latency_s=0.01, canary_passed=True)


def test_stage0_cached_lookup_detects_lateral_move():
    engine = MatchEngine(provider=_CachedMoveProvider())
    decision = engine.resolve_attorney(
        {
            "name": "W. Buechele",
            "firm": "Winstead PC",
            "city": "Dallas",
            "jd_year": 2016,
            "email": "bbuechele@winstead.com",
            "txbar_contact_id": "350258",
        }
    )

    assert decision.search_stage == "cached_lookup"
    assert decision.firm_changed is True
    assert decision.match_status == "firm_change_match"
    assert decision.review_status == "pending_review"


def test_custom_scoring_config_ambiguity_delta():
    input_rec = {"name": "Jane Doe", "firm": "Alpha LLP", "city": "Austin", "jd_year": 2018}
    parsed = parse_input_name(input_rec["name"])

    hit1 = SearchHit(
        contact_id="1", name=NameParts(first_name="Jane", last_name="Doe"), firm="Alpha LLP", city="Austin"
    )
    prof1 = Profile(contact_id="1", name=hit1.name, firm="Alpha LLP", city="Austin", grad_year=2018)
    hit2 = SearchHit(
        contact_id="2", name=NameParts(first_name="Jane", last_name="Doe"), firm="Alpha LLP", city="Austin"
    )
    prof2 = Profile(contact_id="2", name=hit2.name, firm="Alpha LLP", city="Austin", grad_year=2017)

    strict_engine = MatchEngine(scoring_config=ScoringConfig(ambiguity_delta=8.0))
    evals = [
        strict_engine.evaluate_candidate(hit1, input_rec, parsed, None, prof1, stage="s1", rank=1),
        strict_engine.evaluate_candidate(hit2, input_rec, parsed, None, prof2, stage="s1", rank=2),
    ]
    assert strict_engine._classify_decision(evals, "Alpha LLP").match_status == "ambiguous"

    loose_engine = MatchEngine(scoring_config=ScoringConfig(ambiguity_delta=1.0))
    assert loose_engine._classify_decision(evals, "Alpha LLP").match_status == "exact_match"
