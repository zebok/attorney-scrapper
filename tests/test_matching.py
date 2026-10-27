"""Unit tests for entity resolution and match confidence scoring."""

from datetime import date

from txbar.matching import MatchEngine
from txbar.normalize import parse_email, parse_input_name
from txbar.parser import NameParts, Profile, SearchHit


class DummyClient:
    pass


def test_score_exact_match():
    engine = MatchEngine(client=DummyClient())  # type: ignore

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
    engine = MatchEngine(client=DummyClient())  # type: ignore

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
