"""Identity Resolution & Match Scoring Engine.

Implements a multi-stage waterfall search (cached ID -> surname+firm -> name+city -> statewide)
and composite multi-signal scoring driven by ScoringConfig.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from txbar.config import Settings, get_settings
from txbar.normalize import (
    EmailHints,
    ParsedName,
    canonical_firm,
    firm_search_term,
    parse_email,
    parse_input_name,
)
from txbar.provider import DirectoryProvider, Profile, SearchHit
from txbar.rules import KnowledgeBase, get_knowledge_base
from txbar.signals import (
    FirmAffiliationSignal,
    GeographicSignal,
    NameMatchSignal,
    ScoringConfig,
    TemporalCareerSignal,
)

logger = logging.getLogger(__name__)


@dataclass
class CandidateEvaluation:
    contact_id: str
    search_stage: str
    rank: int
    score: float
    score_name: float
    score_firm: float
    score_temporal: float
    score_geo: float
    full_name: str
    preferred_name: str
    firm: str
    city: str
    bar_card_number: str
    license_date: str | None
    license_status: str
    law_school: str
    grad_year: int | None
    explanation: str
    profile: Profile | None = None


@dataclass
class MatchDecision:
    best_candidate: CandidateEvaluation | None
    candidates: list[CandidateEvaluation] = field(default_factory=list)
    match_score: float = 0.0
    score_name: float = 0.0
    score_firm: float = 0.0
    score_temporal: float = 0.0
    score_geo: float = 0.0
    match_status: str = "not_found"
    match_explanation: str = "No matching candidates found."
    firm_changed: bool = False
    search_stage: str = "none"
    review_status: str = "not_found"


class MatchEngine:
    """Configurable Entity Resolution & Match Scoring Engine."""

    def __init__(
        self,
        provider: DirectoryProvider | None = None,
        kb: KnowledgeBase | None = None,
        scoring_config: ScoringConfig | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.provider = provider
        self.kb = kb or get_knowledge_base()
        self.scoring_config = scoring_config or ScoringConfig.from_settings(self.settings)
        self.signal_name = NameMatchSignal()
        self.signal_firm = FirmAffiliationSignal()
        self.signal_temporal = TemporalCareerSignal()
        self.signal_geo = GeographicSignal()

    def evaluate_candidate(
        self,
        hit: SearchHit,
        input_record: dict[str, Any],
        parsed_name: ParsedName,
        email_hints: EmailHints | None,
        profile: Profile | None,
        stage: str,
        rank: int,
    ) -> CandidateEvaluation:
        """Score a candidate profile against the input record using composite signals."""
        cand_name = profile.name if profile else hit.name
        res_name = self.signal_name.evaluate(
            input_record, parsed_name, email_hints, hit, profile, self.kb, self.scoring_config
        )
        res_firm = self.signal_firm.evaluate(
            input_record, parsed_name, email_hints, hit, profile, self.kb, self.scoring_config
        )
        res_temporal = self.signal_temporal.evaluate(
            input_record, parsed_name, email_hints, hit, profile, self.kb, self.scoring_config
        )
        res_geo = self.signal_geo.evaluate(
            input_record, parsed_name, email_hints, hit, profile, self.kb, self.scoring_config
        )

        total_score = min(
            100.0,
            max(0.0, res_name.score + res_firm.score + res_temporal.score + res_geo.score),
        )
        reasons = [
            r.explanation
            for r in (res_name, res_firm, res_temporal, res_geo)
            if r.explanation and not r.explanation.startswith(("No input", "City unspecified"))
        ]

        return CandidateEvaluation(
            contact_id=hit.contact_id,
            search_stage=stage,
            rank=rank,
            score=round(total_score, 1),
            score_name=round(res_name.score, 1),
            score_firm=round(res_firm.score, 1),
            score_temporal=round(res_temporal.score, 1),
            score_geo=round(res_geo.score, 1),
            full_name=cand_name.full_name,
            preferred_name=cand_name.preferred_name.strip(),
            firm=(profile.firm if profile else hit.firm).strip(),
            city=(profile.city if profile else hit.city),
            bar_card_number=(profile.bar_card_number if profile else ""),
            license_date=profile.license_date.isoformat() if (profile and profile.license_date) else None,
            license_status=(profile.license_status if profile else hit.license_status),
            law_school=(profile.law_school if profile else ""),
            grad_year=(profile.grad_year if profile else None),
            explanation="; ".join(reasons),
            profile=profile,
        )

    def _classify_decision(
        self,
        evaluations: list[CandidateEvaluation],
        firm_val: str,
        cached_contact_id: str | None = None,
    ) -> MatchDecision:
        evaluations.sort(key=lambda c: c.score, reverse=True)
        best = evaluations[0]
        cfg = self.scoring_config

        norm_input_firm = canonical_firm(firm_val, kb=self.kb)
        norm_cand_firm = canonical_firm(best.firm, kb=self.kb)
        firm_changed = bool(
            best.score_name >= cfg.weight_name * 0.85
            and norm_input_firm
            and norm_cand_firm
            and norm_input_firm != norm_cand_firm
            and best.score_firm == 0.0
        )

        is_ambiguous = (
            len(evaluations) > 1
            and best.score >= cfg.review_floor
            and abs(best.score - evaluations[1].score) < cfg.ambiguity_delta
        )

        if is_ambiguous:
            match_status = "ambiguous"
            review_status = "pending_review"
            explanation = (
                f"Ambiguous candidates: Top match score {best.score:.1f} vs runner-up "
                f"{evaluations[1].score:.1f}. Sent to human review queue."
            )
        elif firm_changed and best.score >= cfg.review_floor:
            match_status = "firm_change_match"
            review_status = "pending_review" if best.score < cfg.auto_approve_threshold else "auto_approved"
            explanation = (
                f"Lateral move detected! Candidate matches identity but firm changed from "
                f"'{firm_val}' to '{best.firm}'. Confidence: {best.score:.1f}%."
            )
        elif best.score >= cfg.auto_approve_threshold:
            if cached_contact_id:
                match_status = "cached_match"
                explanation = f"Direct cache hit via ContactID {cached_contact_id} ({best.score:.1f}%): {best.explanation}"
            else:
                match_status = "exact_match" if best.score_firm >= cfg.weight_firm else "alias_match"
                explanation = f"High confidence match ({best.score:.1f}%): {best.explanation}"
            review_status = "auto_approved"
        elif best.score >= cfg.review_floor:
            match_status = "cached_match" if cached_contact_id else "low_confidence"
            review_status = "pending_review"
            explanation = (
                f"Moderate confidence ({best.score:.1f}%). Requires human verification: {best.explanation}"
            )
        else:
            match_status = "not_found"
            review_status = "not_found"
            explanation = (
                f"Top candidate score ({best.score:.1f}%) below review threshold ({cfg.review_floor}%)."
            )

        return MatchDecision(
            best_candidate=best,
            candidates=evaluations,
            match_score=best.score,
            score_name=best.score_name,
            score_firm=best.score_firm,
            score_temporal=best.score_temporal,
            score_geo=best.score_geo,
            match_status=match_status,
            match_explanation=explanation,
            firm_changed=firm_changed,
            search_stage=best.search_stage,
            review_status=review_status,
        )

    def resolve_attorney(self, input_record: dict[str, Any]) -> MatchDecision:
        """Execute the multi-stage waterfall search and scoring for an attorney."""
        if self.provider is None:
            raise RuntimeError("MatchEngine requires a DirectoryProvider to resolve attorneys.")

        raw_name = input_record.get("name") or ""
        email_val = input_record.get("email") or ""
        firm_val = input_record.get("firm") or ""
        city_val = input_record.get("city") or ""
        cached_contact_id = input_record.get("txbar_contact_id")

        parsed_name = parse_input_name(raw_name, email=email_val)
        email_hints = parse_email(email_val, parsed_name)

        # Stage 0: Direct Cache Lookup
        if cached_contact_id:
            try:
                prof = self.provider.get_profile(str(cached_contact_id))
                hit = SearchHit(
                    contact_id=prof.contact_id,
                    name=prof.name,
                    firm=prof.firm,
                    city=prof.city,
                    license_status=prof.license_status,
                )
                eval_cand = self.evaluate_candidate(
                    hit, input_record, parsed_name, email_hints, prof, stage="cached_lookup", rank=1
                )
                return self._classify_decision(
                    [eval_cand], firm_val, cached_contact_id=str(cached_contact_id)
                )
            except Exception as e:
                logger.warning(
                    "Cached contact_id %s failed: %s. Falling back to search.", cached_contact_id, e
                )

        seen_contact_ids: set[str] = set()
        search_hits: list[tuple[SearchHit, str]] = []

        def _collect(hits: list[SearchHit], stage_tag: str) -> None:
            for h in hits:
                if h.contact_id not in seen_contact_ids:
                    seen_contact_ids.add(h.contact_id)
                    search_hits.append((h, stage_tag))

        # Stage 1: Last name + Distinctive Firm Core
        firm_term = firm_search_term(firm_val)
        if parsed_name.last and firm_term:
            try:
                _collect(self.provider.search_by_affiliation(parsed_name.last, firm_term), "stage1_last_firm")
            except Exception as e:
                logger.warning("Stage 1 search failed: %s", e)

        has_plausible_hit = False
        if search_hits:
            for h, _ in search_hits:
                eval_name = self.signal_name.evaluate(
                    input_record, parsed_name, email_hints, h, None, self.kb, self.scoring_config
                )
                if eval_name.metadata.get("plausible"):
                    has_plausible_hit = True
                    break
            if not has_plausible_hit:
                search_hits = []

        query_name = (
            parsed_name.last
            if parsed_name.first_is_initial
            else f"{parsed_name.first} {parsed_name.last}".strip()
        )

        # Stage 2: Name + City
        if not has_plausible_hit and city_val:
            try:
                _collect(self.provider.search_by_location(query_name, city_val), "stage2_name_city")
                has_plausible_hit = bool(search_hits)
            except Exception as e:
                logger.warning("Stage 2 search failed: %s", e)

        # Stage 3: Statewide Full Name or Surname Variants
        if not has_plausible_hit and query_name:
            try:
                _collect(self.provider.search_statewide(query_name), "stage3_statewide_fullname")
                has_plausible_hit = bool(search_hits)
            except Exception as e:
                logger.warning("Stage 3 full name search failed: %s", e)

            if not has_plausible_hit:
                for variant in parsed_name.last_variants:
                    try:
                        _collect(self.provider.search_statewide(variant), f"stage3_statewide_{variant}")
                        if search_hits:
                            break
                    except Exception as e:
                        logger.warning("Stage 3 search failed for %s: %s", variant, e)

        if not search_hits:
            return MatchDecision(
                best_candidate=None,
                candidates=[],
                match_score=0.0,
                match_status="not_found",
                match_explanation=f"No candidates found across all waterfall stages for '{raw_name}'.",
                review_status="not_found",
            )

        evaluations: list[CandidateEvaluation] = []
        for rank, (hit, stage_tag) in enumerate(
            search_hits[: self.settings.scraper_max_detail_fetches], start=1
        ):
            try:
                profile = self.provider.get_profile(hit.contact_id)
            except Exception as e:
                logger.warning("Failed fetching profile for %s: %s", hit.contact_id, e)
                profile = None

            evaluations.append(
                self.evaluate_candidate(
                    hit, input_record, parsed_name, email_hints, profile, stage=stage_tag, rank=rank
                )
            )

        return self._classify_decision(evaluations, firm_val)
