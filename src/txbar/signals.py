"""Composite Scoring Strategy for Legal Entity Resolution.

Evaluates 4 isolated, configurable signals driven by ScoringConfig and KnowledgeBase:
1. NameMatchSignal
2. FirmAffiliationSignal
3. TemporalCareerSignal
4. GeographicSignal
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field
from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from txbar.config import Settings
from txbar.normalize import (
    EmailHints,
    ParsedName,
    alpha_key,
    canonical_firm,
    firm_key,
    given_names_compatible,
    strip_accents,
)
from txbar.provider import Profile, SearchHit
from txbar.rules import KnowledgeBase


class ScoringConfig(BaseModel):
    """Configurable weights and thresholds for entity resolution."""

    weight_name: float = Field(default=35.0, description="Max points for name match")
    weight_firm: float = Field(default=25.0, description="Max points for firm match")
    weight_temporal: float = Field(default=25.0, description="Max points for temporal consistency")
    weight_geo: float = Field(default=15.0, description="Max points for geographic alignment")

    auto_approve_threshold: float = Field(default=85.0, description="Minimum score for automatic approval")
    review_floor: float = Field(default=55.0, description="Minimum score to enter human review queue")
    ambiguity_delta: float = Field(default=8.0, description="Score delta triggering review")

    @classmethod
    def from_settings(cls, settings: Settings) -> ScoringConfig:
        return cls(
            weight_name=settings.weight_name,
            weight_firm=settings.weight_firm,
            weight_temporal=settings.weight_temporal,
            weight_geo=settings.weight_geo,
            auto_approve_threshold=settings.match_auto_approve,
            review_floor=settings.match_review_floor,
            ambiguity_delta=settings.match_ambiguity_delta,
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
        reasons: list[str] = []

        cand_name = candidate_profile.name if candidate_profile else candidate_hit.name
        cand_first = cand_name.first_name.strip()
        cand_middle = cand_name.middle_name.strip()
        cand_last = cand_name.last_name.strip()
        cand_pref = cand_name.preferred_name.strip()
        cand_suffix = cand_name.suffix.strip()

        # 1. Last name check (mandatory gate)
        cand_last_key = alpha_key(cand_last)
        last_match = any(
            alpha_key(v) == cand_last_key or JaroWinkler.similarity(alpha_key(v), cand_last_key) > 0.92
            for v in parsed_name.last_variants
        )
        if not last_match and cand_pref and parsed_name.last:
            last_match = alpha_key(parsed_name.last) in alpha_key(cand_pref)

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

        # 3. First Name, Preferred Name & Nickname Matching (delegated to injected kb)
        first_matched = False
        if given_names_compatible(parsed_name.first, cand_first, kb=kb):
            first_matched = True
            reasons.append("First name / nickname match")
        elif cand_pref and given_names_compatible(parsed_name.first, cand_pref, kb=kb):
            first_matched = True
            reasons.append(f"Preferred name match ('{cand_pref}')")
        elif (
            parsed_name.first
            and cand_middle
            and given_names_compatible(parsed_name.first, cand_middle, kb=kb)
        ):
            first_matched = True
            reasons.append("First name matched candidate middle name")

        # 4. Email Hint Verification (Disambiguator)
        email_confirmed = False
        if email_hints and cand_first:
            cand_first_init = cand_first[0].lower()
            cand_mid_init = cand_middle[0].lower() if cand_middle else ""
            if email_hints.initials:
                target_initials = email_hints.initials.lower()
                if len(target_initials) >= 2 and cand_mid_init:
                    if target_initials == f"{cand_first_init}{cand_mid_init}":
                        email_confirmed = True
                        reasons.append("Email initials confirmed")
                elif target_initials[0] == cand_first_init:
                    email_confirmed = True
                    reasons.append("Email initial confirmed")

            if (
                email_hints.middle_initial
                and cand_mid_init
                and cand_mid_init == email_hints.middle_initial.lower()
            ):
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
            score = max_pts * 0.30
            explanation = f"Surname matched but first name differs ('{parsed_name.first}' vs '{cand_first}')"

        return SignalResult(
            signal_name=self.signal_name,
            score=round(score, 2),
            max_weight=max_pts,
            explanation=explanation,
            metadata={"plausible": first_matched or email_confirmed},
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

        norm_input = canonical_firm(input_firm, kb=kb)
        norm_cand = canonical_firm(cand_firm, kb=kb)

        if norm_input == norm_cand:
            is_alias = firm_key(input_firm) != firm_key(cand_firm)
            label = "Firm merger alias matched" if is_alias else "Firm exact match"
            return SignalResult(
                signal_name=self.signal_name,
                score=max_pts,
                max_weight=max_pts,
                explanation=f"{label}: '{cand_firm}'",
            )

        if kb.are_firm_aliases(norm_input, norm_cand):
            return SignalResult(
                signal_name=self.signal_name,
                score=max_pts,
                max_weight=max_pts,
                explanation=f"Firm merger alias matched: '{input_firm}' ~ '{cand_firm}'",
            )

        if email_hints and email_hints.domain:
            canon_from_domain = kb.get_firm_from_domain(email_hints.domain)
            if canon_from_domain and (
                canon_from_domain == norm_cand or kb.are_firm_aliases(canon_from_domain, norm_cand)
            ):
                return SignalResult(
                    signal_name=self.signal_name,
                    score=round(max_pts * 0.95, 2),
                    max_weight=max_pts,
                    explanation=f"Firm confirmed via corporate email domain '@{email_hints.domain}'",
                )

        sim = fuzz.token_sort_ratio(firm_key(input_firm), firm_key(cand_firm))
        if sim >= 80:
            return SignalResult(
                signal_name=self.signal_name,
                score=round(max_pts * (sim / 100.0), 2),
                max_weight=max_pts,
                explanation=f"Firm high fuzzy match ({sim:.0f}%): '{cand_firm}'",
            )

        words_in = set(firm_key(input_firm).split())
        words_cand = set(firm_key(cand_firm).split())
        common = words_in.intersection(words_cand)
        if words_in and (len(common) >= 2 or (len(common) == 1 and len(words_in) == 1)):
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
        except (TypeError, ValueError):
            return SignalResult(
                signal_name=self.signal_name,
                score=max_pts * 0.50,
                max_weight=max_pts,
                explanation="Invalid input JD year format",
            )

        if cand_grad == target_year or cand_lic_year == target_year:
            return SignalResult(
                signal_name=self.signal_name,
                score=max_pts,
                max_weight=max_pts,
                explanation=f"Exact graduation/admission year match ({target_year})",
            )

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
        input_city = strip_accents((target_record.get("city") or "").strip()).lower()
        cand_city = strip_accents(
            (candidate_profile.city if candidate_profile else candidate_hit.city).strip()
        ).lower()

        if not input_city or not cand_city:
            return SignalResult(
                signal_name=self.signal_name,
                score=round(max_pts * 0.33, 2),
                max_weight=max_pts,
                explanation="City unspecified (statewide match)",
            )

        if input_city == cand_city:
            return SignalResult(
                signal_name=self.signal_name,
                score=max_pts,
                max_weight=max_pts,
                explanation=f"Same city: '{cand_city.title()}'",
            )

        metro = kb.get_metro(input_city)
        if metro and kb.same_metro(input_city, cand_city):
            return SignalResult(
                signal_name=self.signal_name,
                score=round(max_pts * 0.90, 2),
                max_weight=max_pts,
                explanation=f"Same metropolitan statistical area ({metro.upper()} cluster: '{input_city.title()}' ~ '{cand_city.title()}')",
            )

        return SignalResult(
            signal_name=self.signal_name,
            score=round(max_pts * 0.33, 2),
            max_weight=max_pts,
            explanation=f"Different city in Texas: '{input_city.title()}' vs '{cand_city.title()}'",
        )
