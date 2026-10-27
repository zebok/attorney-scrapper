"""Unit tests for the modular entity matching signals."""

from datetime import date

from txbar.normalize import parse_email, parse_input_name
from txbar.parser import NameParts, Profile, SearchHit
from txbar.rules import get_knowledge_base
from txbar.signals import (
    FirmAffiliationSignal,
    GeographicSignal,
    NameMatchSignal,
    ScoringConfig,
    TemporalCareerSignal,
)


def test_name_signal_nicknames_and_preferred():
    sig = NameMatchSignal()
    kb = get_knowledge_base()
    cfg = ScoringConfig(weight_name=35.0)

    # 1. Nickname: Samuel <-> Sam
    input_rec = {"name": "Samuel Spiers"}
    p_name = parse_input_name("Samuel Spiers")
    hit = SearchHit(contact_id="1", name=NameParts(first_name="Sam", last_name="Spiers"))
    res = sig.evaluate(input_rec, p_name, None, hit, None, kb, cfg)
    assert res.score == 35.0
    assert "Full name matched" in res.explanation

    # 2. Preferred name: William Bryant Buechele -> Bryant Buechele
    p_name2 = parse_input_name("Bryant Buechele")
    hit2 = SearchHit(
        contact_id="2",
        name=NameParts(
            first_name="William", middle_name="Bryant", last_name="Buechele", preferred_name="Bryant Buechele"
        ),
    )
    res2 = sig.evaluate({"name": "Bryant Buechele"}, p_name2, None, hit2, None, kb, cfg)
    assert res2.score == 35.0

    # 3. Generational suffix mismatch penalty
    p_name3 = parse_input_name("Wayne Zeringue Jr.")
    hit3 = SearchHit(contact_id="3", name=NameParts(first_name="Wayne", last_name="Zeringue", suffix="III"))
    res3 = sig.evaluate({"name": "Wayne Zeringue Jr."}, p_name3, None, hit3, None, kb, cfg)
    assert res3.score == 0.0
    assert "Generational suffix mismatch" in res3.explanation


def test_firm_signal_merger_and_domain():
    sig = FirmAffiliationSignal()
    kb = get_knowledge_base()
    cfg = ScoringConfig(weight_firm=25.0)

    # 1. Exact match
    input_rec = {"firm": "Jackson Walker LLP"}
    hit = SearchHit(contact_id="1", name=NameParts(), firm="Jackson Walker L.L.P.")
    res = sig.evaluate(input_rec, None, None, hit, None, kb, cfg)
    assert res.score == 25.0

    # 2. Merger alias: Perkins Coie ~ Ashurst Perkins Coie
    input_rec2 = {"firm": "Perkins Coie"}
    hit2 = SearchHit(contact_id="2", name=NameParts(), firm="Ashurst Perkins Coie")
    res2 = sig.evaluate(input_rec2, None, None, hit2, None, kb, cfg)
    assert res2.score == 25.0
    assert "merger alias" in res2.explanation.lower()

    # 3. Email domain mapping: jw.com -> jackson walker
    p_name = parse_input_name("Jennifer Freel")
    hints = parse_email("jfreel@jw.com", p_name)
    hit3 = SearchHit(contact_id="3", name=NameParts(), firm="Jackson Walker")
    res3 = sig.evaluate({"firm": "Other Name"}, p_name, hints, hit3, None, kb, cfg)
    assert res3.score >= 23.0
    assert "@jw.com" in res3.explanation


def test_temporal_signal_tolerance():
    sig = TemporalCareerSignal()
    kb = get_knowledge_base()
    cfg = ScoringConfig(weight_temporal=25.0)

    # Exact year
    prof = Profile(contact_id="1", name=NameParts(), grad_year=2018, license_date=date(2018, 11, 2))
    res = sig.evaluate(
        {"jd_year": 2018}, None, None, SearchHit(contact_id="1", name=NameParts()), prof, kb, cfg
    )
    assert res.score == 25.0

    # Divergence
    prof_old = Profile(contact_id="2", name=NameParts(), grad_year=1995, license_date=date(1995, 11, 2))
    res_div = sig.evaluate(
        {"jd_year": 2018}, None, None, SearchHit(contact_id="2", name=NameParts()), prof_old, kb, cfg
    )
    assert res_div.score == 0.0


def test_geo_signal_metro_clustering():
    sig = GeographicSignal()
    kb = get_knowledge_base()
    cfg = ScoringConfig(weight_geo=15.0)

    # Same city
    hit1 = SearchHit(contact_id="1", name=NameParts(), city="Austin")
    assert sig.evaluate({"city": "Austin"}, None, None, hit1, None, kb, cfg).score == 15.0

    # Metro cluster: Plano and Dallas are both DFW
    hit2 = SearchHit(contact_id="2", name=NameParts(), city="Plano")
    res_metro = sig.evaluate({"city": "Dallas"}, None, None, hit2, None, kb, cfg)
    assert res_metro.score >= 13.0
    assert "DFW" in res_metro.explanation

    # Different cities
    hit3 = SearchHit(contact_id="3", name=NameParts(), city="Houston")
    res_diff = sig.evaluate({"city": "Dallas"}, None, None, hit3, None, kb, cfg)
    assert res_diff.score <= 5.0
