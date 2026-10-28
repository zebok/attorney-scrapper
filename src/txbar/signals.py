"""Composite Scoring Strategy for Legal Entity Resolution.

Decomposes monolithic matching logic into isolated, testable, and configurable signals:
1. `NameMatchSignal` (Name similarity, nicknames, middle initials, suffixes, email hints)
2. `FirmAffiliationSignal` (Firm core, merger aliases, corporate suffixes, domain mapping)
3. `TemporalCareerSignal` (Admission year vs graduation year plausibility)
4. `GeographicSignal` (City jurisdiction and metropolitan statistical clusters)

Weights and decision thresholds are externally driven via `ScoringConfig`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field
from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from txbar.normalize import (
    EmailHints,
    ParsedName,
    alpha_key,
    canonical_firm,
    firm_key,
    given_names_compatible,
)
from txbar.parser import Profile, SearchHit
from txbar.rules import KnowledgeBase


class ScoringConfig(BaseModel):
    """Configurable weights and thresholds for entity resolution."""

    weight_name: float = Field(default=35.0, description="Max points for name match")
    weight_firm: float = Field(default=25.0, description="Max points for firm match")
    weight_temporal: float = Field(default=25.0, description="Max points for temporal consistency")
    weight_geo: float = Field(default=15.0, description="Max points for geographic alignment")

    auto_approve_threshold: float = Field(
        default=85.0, description="Minimum score for automatic master merge"
    )
    review_floor: float = Field(default=55.0, description="Minimum score to enter human review queue")
    ambiguity_delta: float = Field(
        default=8.0, description="Score delta between top candidates triggering review"
    )


@dataclass
class SignalResult:
    """Outcome of an individual match signal evaluation."""

    signal_name: str
    score: float
    max_weight: float
    explanation: str
    metadata: dict[str, Any] = field(default_factory=dict)


class MatchSignal(Protocol):
    """Abstract interface for entity matching signals."""

    @property
    def signal_name(self) -> str: ...

    def evaluate(
        self,
        target_record: dict[str, Any],
        parsed_name: ParsedName,
        email_hints: EmailHints | None,
        candidate_hit: SearchHit,
        candidate_profile: Profile | None,
        kb: KnowledgeBase,
        config: ScoringConfig,
    ) -> SignalResult: ...


class NameMatchSignal:
    """Evaluates name similarity, nicknames, middle initials, suffixes, and email hints."""

    @property
    def signal_name(self) -> str:
        return "name"

    def evaluate(
        self,
        target_record: dict[str, Any],
        parsed_name: ParsedName,
        email_hints: EmailHints | None,
        candidate_hit: SearchHit,
        candidate_profile: Profile | None,
        kb: KnowledgeBase,
        config: ScoringConfig,
    ) -> SignalResult:
        max_pts = config.weight_name
        score = 0.0
        reasons: list[str] = []

        cand_first = (
            candidate_profile.name.first_name if candidate_profile else candidate_hit.name.first_name
        ).strip()
        cand_middle = (
            candidate_profile.name.middle_name if candidate_profile else candidate_hit.name.middle_name
        ).strip()
        cand_last = (
            candidate_profile.name.last_name if candidate_profile else candidate_hit.name.last_name
        ).strip()
        cand_pref = (
            candidate_profile.name.preferred_name if candidate_profile else candidate_hit.name.preferred_name
        ).strip()
        cand_suffix = (
            candidate_profile.name.suffix if candidate_profile else candidate_hit.name.suffix
        ).strip()

        # 1. Last name check (mandatory gate for name score)
        last_match = False
        for v in parsed_name.last_variants:
            if (
                alpha_key(v) == alpha_key(cand_last)
                or JaroWinkler.similarity(alpha_key(v), alpha_key(cand_last)) > 0.92
            ):
                last_match = True
                break

        if not last_match:
            # Check preferred name or hyphenated compound matches
            if cand_pref and parsed_name.last and alpha_key(parsed_name.last) in alpha_key(cand_pref):
                last_match = True

        if not last_match:
            return SignalResult(
                signal_name=self.signal_name,
                score=0.0,
                max_weight=max_pts,
                explanation="Last name mismatch",
            )

        # 2. Suffix validation (Jr, III, IV)
        if parsed_name.suffix and cand_suffix:
            if alpha_key(parsed_name.suffix) == alpha_key(cand_suffix):
                reasons.append(f"Suffix matched ({cand_suffix})")
            else:
                return SignalResult(
                    signal_name=self.signal_name,
                    score=0.0,
                    max_weight=max_pts,
                    explanation=f"Generational suffix mismatch: '{parsed_name.suffix}' vs '{cand_suffix}'",
                )

        # 3. First Name & Nickname Matching
        first_matched = False
        # Direct match or initial
        if given_names_compatible(parsed_name.first, cand_first):
            first_matched = True
            reasons.append("First name / nickname match")
        elif cand_pref and given_names_compatible(parsed_name.first, cand_pref):
            first_matched = True
            reasons.append(f"Preferred name match ('{cand_pref}')")
        elif kb.are_nicknames(parsed_name.first, cand_first):
            first_matched = True
            reasons.append(f"Nickname cluster match ({parsed_name.first} ~ {cand_first})")

        # Inverted first/middle check
        if not first_matched and parsed_name.first and cand_middle:
            if given_names_compatible(parsed_name.first, cand_middle):
                first_matched = True
                reasons.append("First name matched candidate middle name")

        # 4. Email Hint Verification (Disambiguator)
        email_confirmed = False
        if email_hints:
            if email_hints.initials and cand_first:
                cand_given_initials = (
                    cand_first[0] + (cand_middle[0] if cand_middle else "")
                ).lower()
                target_initials = email_hints.initials.lower()
                if (
                    cand_given_initials.startswith(target_initials)
                    or target_initials.startswith(cand_given_initials[0])
                ):
                    email_confirmed = True
                    reasons.append("Email initials confirmed")

            if email_hints.middle_initial and cand_middle:
                if cand_middle[0].lower() == email_hints.middle_initial.lower():
                    email_confirmed = True
                    reasons.append(
                        f"Middle initial '{email_hints.middle_initial.upper()}' confirmed by corporate email"
                    )

        if first_matched:
            score = max_pts
            explanation = "Full name matched (" + ", ".join(reasons) + ")"
        elif email_confirmed:
            score = max_pts * 0.85
            explanation = "Surname and email hint matched (" + ", ".join(reasons) + ")"
        else:
            # Surnames matched but given name diverged
            score = max_pts * 0.30
            explanation = f"Surname matched but first name differs ('{parsed_name.first}' vs '{cand_first}')"

        return SignalResult(
            signal_name=self.signal_name,
            score=round(score, 2),
            max_weight=max_pts,
            explanation=explanation,
        )


class FirmAffiliationSignal:
    """Evaluates law firm core similarity, merger aliases, and corporate email domains."""

    @property
    def signal_name(self) -> str:
        return "firm"

    def evaluate(
        self,
        target_record: dict[str, Any],
        parsed_name: ParsedName,
        email_hints: EmailHints | None,
        candidate_hit: SearchHit,
        candidate_profile: Profile | None,
        kb: KnowledgeBase,
        config: ScoringConfig,
    ) -> SignalResult:
        max_pts = config.weight_firm
        input_firm = (target_record.get("firm") or "").strip()
        cand_firm = (candidate_profile.firm if candidate_profile else candidate_hit.firm).strip()

        if not input_firm:
            return SignalResult(
                signal_name=self.signal_name,
                score=0.0,
                max_weight=max_pts,
                explanation="Firm unspecified",
            )

        if not cand_firm:
            return SignalResult(
                signal_name=self.signal_name,
                score=round(max_pts * 0.50, 2),
                max_weight=max_pts,
                explanation="Firm not listed on Bar profile",
            )

        norm_input = canonical_firm(input_firm)
        norm_cand = canonical_firm(cand_firm)

        # 1. Exact canonical or resolved alias match
        if norm_input == norm_cand:
            is_alias = firm_key(input_firm) != firm_key(cand_firm)
            label = "Firm merger alias matched" if is_alias else "Firm exact match"
            return SignalResult(
                signal_name=self.signal_name,
                score=max_pts,
                max_weight=max_pts,
                explanation=f"{label}: '{cand_firm}'",
            )

        # 2. Merger / Alias equivalence via KnowledgeBase
        if kb.are_firm_aliases(norm_input, norm_cand):
            return SignalResult(
                signal_name=self.signal_name,
                score=max_pts,
                max_weight=max_pts,
                explanation=f"Firm merger alias matched: '{input_firm}' ~ '{cand_firm}'",
            )

        # 3. Domain confirmation via KnowledgeBase
        if email_hints and email_hints.domain:
            canon_from_domain = kb.get_firm_from_domain(email_hints.domain)
            if canon_from_domain and (
                canon_from_domain == norm_cand or kb.are_firm_aliases(canon_from_domain, norm_cand)
            ):
                return SignalResult(
                    signal_name=self.signal_name,
                    score=max_pts * 0.95,
                    max_weight=max_pts,
                    explanation=f"Firm confirmed via corporate email domain '@{email_hints.domain}'",
                )

        # 4. Fuzzy token sort ratio
        sim = fuzz.token_sort_ratio(firm_key(input_firm), firm_key(cand_firm))
        if sim >= 80:
            return SignalResult(
                signal_name=self.signal_name,
                score=round(max_pts * (sim / 100.0), 2),
                max_weight=max_pts,
                explanation=f"Firm high fuzzy match ({sim:.0f}%): '{cand_firm}'",
            )

        # 5. Distinctive word overlap
        words_in = set(firm_key(input_firm).split())
        words_cand = set(firm_key(cand_firm).split())
        common = words_in.intersection(words_cand)
        if len(common) >= 2 or (len(common) == 1 and len(words_in) == 1):
            overlap_pct = (len(common) / len(words_in)) * 100
            awarded = max_pts * 0.80 if overlap_pct >= 50 else max_pts * 0.50
            return SignalResult(
                signal_name=self.signal_name,
                score=round(awarded, 2),
                max_weight=max_pts,
                explanation=f"Firm partial overlap ({overlap_pct:.0f}%): '{cand_firm}'",
            )

        return SignalResult(
            signal_name=self.signal_name,
            score=0.0,
            max_weight=max_pts,
            explanation=f"Firm distinct: '{input_firm}' vs '{cand_firm}'",
        )


class TemporalCareerSignal:
    """Cross-references law school graduation year with State Bar admission timeline."""

    @property
    def signal_name(self) -> str:
        return "temporal"

    def evaluate(
        self,
        target_record: dict[str, Any],
        parsed_name: ParsedName,
        email_hints: EmailHints | None,
        candidate_hit: SearchHit,
        candidate_profile: Profile | None,
        kb: KnowledgeBase,
        config: ScoringConfig,
    ) -> SignalResult:
        max_pts = config.weight_temporal
        input_jd = target_record.get("jd_year")
        cand_grad = candidate_profile.grad_year if candidate_profile else None
        cand_lic_date = candidate_profile.license_date if candidate_profile else None
        cand_lic_year = cand_lic_date.year if cand_lic_date else None

        if not input_jd:
            return SignalResult(
                signal_name=self.signal_name,
                score=max_pts * 0.50,
                max_weight=max_pts,
                explanation="No input JD year to verify",
            )

        try:
            target_year = int(input_jd)
        except Exception:
            return SignalResult(
                signal_name=self.signal_name,
                score=max_pts * 0.50,
                max_weight=max_pts,
                explanation="Invalid input JD year format",
            )

        # 1. Exact match with graduation year or admission year
        if cand_grad == target_year or cand_lic_year == target_year:
            return SignalResult(
                signal_name=self.signal_name,
                score=max_pts,
                max_weight=max_pts,
                explanation=f"Exact graduation/admission year match ({target_year})",
            )

        # 2. Candidate graduation or admission within +-1 year of target JD
        if (cand_grad and abs(cand_grad - target_year) <= 1) or (
            cand_lic_year and abs(cand_lic_year - target_year) <= 1
        ):
            matched_yr = cand_grad if (cand_grad and abs(cand_grad - target_year) <= 1) else cand_lic_year
            return SignalResult(
                signal_name=self.signal_name,
                score=round(max_pts * 0.90, 2),
                max_weight=max_pts,
                explanation=f"Graduation/admission year {matched_yr} aligns closely with JD {target_year} (+-1 yr)",
            )

        # 3. Candidate graduation within +-2 to +-3 years
        ref_year = cand_grad or cand_lic_year
        if ref_year:
            delta = abs(ref_year - target_year)
            if delta == 2:
                return SignalResult(
                    signal_name=self.signal_name,
                    score=round(max_pts * 0.65, 2),
                    max_weight=max_pts,
                    explanation=f"Moderate graduation delta (+-2 years: {ref_year} vs {target_year})",
                )
            if delta <= 3:
                return SignalResult(
                    signal_name=self.signal_name,
                    score=round(max_pts * 0.40, 2),
                    max_weight=max_pts,
                    explanation=f"Moderate graduation delta (+-{delta} years)",
                )

        # Anomaly / divergence (handled smoothly for empirical JD=2017 partner phenomenon)
        return SignalResult(
            signal_name=self.signal_name,
            score=0.0,
            max_weight=max_pts,
            explanation=f"Timeline divergence: Target JD {target_year} vs TX Admission {cand_lic_year}",
        )


class GeographicSignal:
    """Validates practice city and metropolitan cluster jurisdiction."""

    @property
    def signal_name(self) -> str:
        return "geo"

    def evaluate(
        self,
        target_record: dict[str, Any],
        parsed_name: ParsedName,
        email_hints: EmailHints | None,
        candidate_hit: SearchHit,
        candidate_profile: Profile | None,
        kb: KnowledgeBase,
        config: ScoringConfig,
    ) -> SignalResult:
        max_pts = config.weight_geo
        input_city = (target_record.get("city") or "").strip().lower()
        cand_city = (candidate_profile.city if candidate_profile else candidate_hit.city).strip().lower()

        if not input_city or not cand_city:
            return SignalResult(
                signal_name=self.signal_name,
                score=max_pts * 0.33,
                max_weight=max_pts,
                explanation="City unspecified (statewide match)",
            )

        # 1. Exact city match
        if input_city == cand_city:
            return SignalResult(
                signal_name=self.signal_name,
                score=max_pts,
                max_weight=max_pts,
                explanation=f"Same city: '{cand_city.title()}'",
            )

        # 2. Same metropolitan cluster (e.g. Dallas <-> Plano, Houston <-> The Woodlands)
        if kb.same_metro(input_city, cand_city):
            return SignalResult(
                signal_name=self.signal_name,
                score=max_pts * 0.90,
                max_weight=max_pts,
                explanation=f"Same metropolitan statistical area ({kb.get_metro(input_city).upper()} cluster: '{input_city.title()}' ~ '{cand_city.title()}')",
            )

        return SignalResult(
            signal_name=self.signal_name,
            score=max_pts * 0.33,
            max_weight=max_pts,
            explanation=f"Different city in Texas: '{input_city.title()}' vs '{cand_city.title()}'",
        )
