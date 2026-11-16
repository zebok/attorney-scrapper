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


def test_email_middle_initial_disambiguation():
    sig = NameMatchSignal()
    kb = get_knowledge_base()
    cfg = ScoringConfig(weight_name=35.0)

    p_name = parse_input_name("Z. Roberts")
    hints = parse_email("jrroberts@firm.com", p_name)

    match_hit = SearchHit(
        contact_id="1", name=NameParts(first_name="John", middle_name="Robert", last_name="Roberts")
    )
    mismatch_hit = SearchHit(
        contact_id="2", name=NameParts(first_name="John", middle_name="Adam", last_name="Roberts")
    )

    res_match = sig.evaluate({}, p_name, hints, match_hit, None, kb, cfg)
    res_mismatch = sig.evaluate({}, p_name, hints, mismatch_hit, None, kb, cfg)

    assert res_match.metadata["plausible"] is True
    assert res_mismatch.metadata["plausible"] is False
    assert res_match.score > res_mismatch.score


def test_custom_kb_and_accent_metro_normalization(tmp_path):
    (tmp_path / "firm_aliases.json").write_text('{"acme legal": ["old acme"]}', encoding="utf-8")
    (tmp_path / "firm_domains.json").write_text('{"acme.com": "acme legal"}', encoding="utf-8")
    (tmp_path / "metro_areas.json").write_text('{"MET": ["san jose", "palo alto"]}', encoding="utf-8")
    (tmp_path / "nicknames.json").write_text('[["alexander", "sasha"]]', encoding="utf-8")

    from txbar.rules import KnowledgeBase

    custom_kb = KnowledgeBase(rules_dir=tmp_path)
    cfg = ScoringConfig()

    name_res = NameMatchSignal().evaluate(
        {},
        parse_input_name("Sasha Kim"),
        None,
        SearchHit(contact_id="1", name=NameParts(first_name="Alexander", last_name="Kim")),
        None,
        custom_kb,
        cfg,
    )
    assert name_res.score == cfg.weight_name

    firm_res = FirmAffiliationSignal().evaluate(
        {"firm": "Old Acme LLP"},
        None,
        None,
        SearchHit(contact_id="1", name=NameParts(), firm="Acme Legal"),
        None,
        custom_kb,
        cfg,
    )
    assert firm_res.score == cfg.weight_firm

    geo_res = GeographicSignal().evaluate(
        {"city": "San José"},
        None,
        None,
        SearchHit(contact_id="1", name=NameParts(), city="Pálo Alto"),
        None,
        custom_kb,
        cfg,
    )
    assert geo_res.score == round(cfg.weight_geo * 0.90, 2)
    assert "MET" in geo_res.explanation
