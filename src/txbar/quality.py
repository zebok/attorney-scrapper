"""Data Quality (DQ) Rules, Record Validation, and Run-Level Quality Gate.

Addresses Question 5:
- Level 1: Field-level schema & formatting (8-digit bar card string, E.164 phone, valid status).
- Level 2: Record-level business cross-validation (license vs grad date, non-future dates).
- Level 3: Run-level anomaly detection & quality gate (missing phone surge, not-found spike).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from txbar.config import Settings, get_settings

VALID_LICENSE_STATUSES = {
    "Eligible to Practice in Texas",
    "Not Eligible to Practice in Texas",
    "Non-Practicing",
    "Inactive",
    "Deceased",
    "Suspended",
    "Disbarred",
}


@dataclass
class RecordDQResult:
    is_valid: bool
    issues: list[str] = field(default_factory=list)


def validate_enriched_record(record: dict[str, Any]) -> RecordDQResult:
    """Validate a single enriched record against business and schema rules."""
    issues: list[str] = []

    # 1. Bar Card Number check: must be exactly 8 digits string
    bar_card = record.get("bar_card_number")
    if bar_card and not re.fullmatch(r"\d{8}", str(bar_card)):
        issues.append(f"INVALID_BAR_CARD_FORMAT:{bar_card}")

    # 2. Phone check: if present, must be formatted as E.164 (+1XXXXXXXXXX)
    phone = record.get("phone")
    if phone and not re.fullmatch(r"\+1[2-9]\d{9}", str(phone)):
        issues.append(f"MALFORMED_PHONE:{phone}")

    # 3. License Status check: must belong to official State Bar catalog
    status = record.get("license_status")
    if status and status not in VALID_LICENSE_STATUSES:
        issues.append(f"UNKNOWN_LICENSE_STATUS:{status}")

    # 4. License Date check: cannot be in the future
    lic_date = record.get("license_date")
    if lic_date:
        if isinstance(lic_date, str):
            try:
                lic_date = date.fromisoformat(lic_date)
            except Exception:
                issues.append("INVALID_LICENSE_DATE_FORMAT")
        if isinstance(lic_date, date) and lic_date > date.today():
            issues.append(f"FUTURE_LICENSE_DATE:{lic_date}")

    # 5. Temporal plausibility: admission year vs graduation year
    grad_year = record.get("grad_year")
    if grad_year and isinstance(lic_date, date):
        # Attorney should be admitted around or after grad year (within -1 to +5 years typical)
        if lic_date.year < (grad_year - 1):
            issues.append(f"TEMPORAL_ANOMALY:admitted_{lic_date.year}_before_grad_{grad_year}")

    return RecordDQResult(is_valid=(len(issues) == 0), issues=issues)


@dataclass
class RunDQReport:
    total_records: int
    auto_approved: int
    pending_review: int
    not_found: int
    errors: int
    missing_phone_count: int
    missing_phone_ratio: float
    not_found_ratio: float
    gate_passed: bool
    gate_violations: list[str]
    rule_summary: dict[str, int]


def evaluate_run_quality_gate(results: list[dict[str, Any]], settings: Settings | None = None) -> RunDQReport:
    """Evaluate batch-level anomaly metrics and determine if the data quality gate passes."""
    s = settings or get_settings()
    total = len(results)
    if total == 0:
        return RunDQReport(
            total_records=0,
            auto_approved=0,
            pending_review=0,
            not_found=0,
            errors=0,
            missing_phone_count=0,
            missing_phone_ratio=0.0,
            not_found_ratio=0.0,
            gate_passed=True,
            gate_violations=[],
            rule_summary={},
        )

    auto_approved = sum(1 for r in results if r.get("review_status") == "auto_approved")
    pending_review = sum(1 for r in results if r.get("review_status") == "pending_review")
    not_found = sum(1 for r in results if r.get("review_status") == "not_found")
    errors = sum(1 for r in results if r.get("match_status") == "error")

    found_records = [r for r in results if r.get("contact_id")]
    missing_phone = sum(1 for r in found_records if not r.get("phone"))
    missing_phone_ratio = (missing_phone / len(found_records)) if found_records else 0.0
    not_found_ratio = not_found / total

    violations: list[str] = []
    if not_found_ratio > s.dq_max_not_found_ratio:
        violations.append(
            f"NOT_FOUND_RATIO_EXCEEDED: {not_found_ratio:.1%} > {s.dq_max_not_found_ratio:.1%}. "
            "Possible directory layout drift or anti-bot obstruction."
        )

    if missing_phone_ratio > s.dq_max_missing_phone_ratio:
        violations.append(
            f"MISSING_PHONE_RATIO_EXCEEDED: {missing_phone_ratio:.1%} > {s.dq_max_missing_phone_ratio:.1%}. "
            "Telephone parsing degradation detected."
        )

    # Aggregate individual rule issues
    rule_counts: dict[str, int] = {}
    for r in results:
        issues_str = r.get("dq_issues") or ""
        if issues_str:
            for issue in issues_str.split("|"):
                rule_key = issue.split(":")[0]
                rule_counts[rule_key] = rule_counts.get(rule_key, 0) + 1

    gate_passed = len(violations) == 0

    return RunDQReport(
        total_records=total,
        auto_approved=auto_approved,
        pending_review=pending_review,
        not_found=not_found,
        errors=errors,
        missing_phone_count=missing_phone,
        missing_phone_ratio=round(missing_phone_ratio, 3),
        not_found_ratio=round(not_found_ratio, 3),
        gate_passed=gate_passed,
        gate_violations=violations,
        rule_summary=rule_counts,
    )
