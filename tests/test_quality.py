"""Unit tests for the Data Quality validation framework and run quality gate."""

from datetime import date

from txbar.quality import evaluate_run_quality_gate, validate_enriched_record


def test_field_level_validations():
    # Valid record
    rec_valid = {
        "bar_card_number": "24101572",
        "phone": "+12147455214",
        "license_status": "Eligible to Practice in Texas",
        "license_date": date(2016, 11, 4),
        "grad_year": 2016,
    }
    res = validate_enriched_record(rec_valid)
    assert res.is_valid is True
    assert len(res.issues) == 0

    # Invalid bar card format and bad phone
    rec_bad = {
        "bar_card_number": "123",  # Not 8 digits
        "phone": "555-1234",  # Not E.164
        "license_status": "MadeUpStatus",
        "license_date": date(2099, 1, 1),  # Future date
    }
    res_bad = validate_enriched_record(rec_bad)
    assert res_bad.is_valid is False
    assert any("INVALID_BAR_CARD_FORMAT" in i for i in res_bad.issues)
    assert any("MALFORMED_PHONE" in i for i in res_bad.issues)
    assert any("UNKNOWN_LICENSE_STATUS" in i for i in res_bad.issues)
    assert any("FUTURE_LICENSE_DATE" in i for i in res_bad.issues)


def test_run_quality_gate_evaluator():
    # Healthy batch: 90 auto approved, 10 pending review, low missing phone
    healthy_results = [
        {"review_status": "auto_approved", "contact_id": f"c_{i}", "phone": "+12147455214"} for i in range(90)
    ] + [
        {"review_status": "pending_review", "contact_id": f"c_{i + 90}", "phone": "+12147455214"}
        for i in range(10)
    ]
    rep = evaluate_run_quality_gate(healthy_results)
    assert rep.gate_passed is True
    assert len(rep.gate_violations) == 0

    # Unhealthy batch: 40% not found (exceeds 20% threshold)
    bad_results = [
        {"review_status": "auto_approved", "contact_id": f"c_{i}", "phone": "+12147455214"} for i in range(60)
    ] + [{"review_status": "not_found", "contact_id": None, "phone": None} for i in range(40)]
    rep_bad = evaluate_run_quality_gate(bad_results)
    assert rep_bad.gate_passed is False
    assert any("NOT_FOUND_RATIO_EXCEEDED" in v for v in rep_bad.gate_violations)
