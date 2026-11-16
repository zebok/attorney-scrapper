"""Parser tests using real captured Texas Bar HTML fixtures."""

from pathlib import Path

import pytest

from txbar.parser import DomDriftError, parse_profile, parse_search_results

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_parse_buechele_profile():
    html = (FIXTURES_DIR / "detail_buechele.html").read_text(encoding="utf-8")
    prof = parse_profile(html, contact_id="350258")

    assert prof.contact_id == "350258"
    assert prof.bar_card_number == "24101572"
    assert prof.name.first_name == "William"
    assert prof.name.middle_name == "Bryant"
    assert prof.name.last_name == "Buechele"
    assert prof.name.preferred_name == "Bryant Buechele"
    assert prof.license_status == "Eligible to Practice in Texas"
    assert prof.firm == "Winstead PC"
    assert prof.city == "Dallas"
    assert prof.grad_year == 2016
    assert prof.license_date.isoformat() == "2016-11-04"
    assert "Real Estate" in prof.practice_areas


def test_parse_zeringue_profile():
    html = (FIXTURES_DIR / "detail_zeringue.html").read_text(encoding="utf-8")
    prof = parse_profile(html, contact_id="355079")

    assert prof.name.first_name == "Wayne"
    assert prof.name.last_name == "Zeringue"
    assert prof.name.suffix == "III"
    assert prof.name.preferred_name == "Jack"
    assert prof.firm == "Sidley Austin LLP"
    assert prof.bar_card_number == "24106620"
    assert prof.grad_year == 2017


def test_parse_callarman_profile_missing_phone():
    html = (FIXTURES_DIR / "detail_callarman.html").read_text(encoding="utf-8")
    prof = parse_profile(html, contact_id="334389")

    assert prof.name.first_name == "Zackary"
    assert prof.name.last_name == "Callarman"
    # The page renders "tel:+--" which normalizer will reject
    assert prof.bar_card_number == "24087248"
    assert prof.grad_year == 2013


def test_parse_search_hits_michael_roberts():
    html = (FIXTURES_DIR / "search_michael_roberts_dallas.html").read_text(encoding="utf-8")
    hits, has_more = parse_search_results(html)

    assert len(hits) == 5
    # One hit is Michael Lee Roberts (Roberts Law Firm)
    assert any(h.contact_id == "288529" for h in hits)
    # Another hit is Michael L. Roberts (Sidley Austin, LLP) -> contact_id 354809
    assert any(h.contact_id == "354809" for h in hits)


def test_parse_search_no_results():
    html = (FIXTURES_DIR / "search_no_results.html").read_text(encoding="utf-8")
    hits, has_more = parse_search_results(html)
    assert len(hits) == 0
    assert has_more is False


def test_dom_drift_error_on_tampered_html():
    tampered_html = "<html><body><h1>Maintenance Page</h1><p>Under construction</p></body></html>"
    with pytest.raises(DomDriftError):
        parse_search_results(tampered_html)
