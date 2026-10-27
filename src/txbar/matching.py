"""Identity Resolution & Match Scoring Engine.

Implements a multi-stage waterfall search and hybrid scoring model:
- Waterfall stages:
  1. Direct lookup if txbar_contact_id or bar_card_number is cached.
  2. Targeted search: Name = last_name, CompanyName = firm_search_term (fast, high precision).
  3. City search: Name = parsed_name, City = city.
  4. Statewide relaxation: Name = last_name (handles lateral moves and suburban offices).
- Multi-signal scoring (0 to 100):
  * Name & Nickname & Preferred Name + Email Hints (35 pts)
  * Firm Match & Merger Aliases & Domain Match (25 pts)
  * Temporal Consistency (License Date / Grad Year vs JD Year) (25 pts)
  * Geographic / Metro Match (15 pts)
- Status Semaphore:
  * auto_approved: score >= 85
  * pending_review: 55 <= score < 85 (or ambiguous candidates / lateral firm changes)
  * not_found: score < 55
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
from txbar.parser import Profile, SearchHit
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
    """Configurable, modular Entity Resolution & Match Scoring Engine."""

    def __init__(
        self,
        client: Any | None = None,
        provider: Any | None = None,
        kb: KnowledgeBase | None = None,
        scoring_config: ScoringConfig | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or provider
        self.provider = provider or self.client
        self.kb = kb or get_knowledge_base()
        self.scoring_config = scoring_config or ScoringConfig(
            weight_name=35.0,
            weight_firm=25.0,
            weight_temporal=25.0,
            weight_geo=15.0,
            auto_approve_threshold=self.settings.match_auto_approve,
            review_floor=self.settings.match_review_floor,
        )
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
        """Score an individual candidate profile against the input record using composite signals."""
        cand_pref = (profile.name.preferred_name if profile else hit.name.preferred_name).strip()
        cand_full = profile.name.full_name if profile else hit.name.full_name
        cand_firm = (profile.firm if profile else hit.firm).strip()
        cand_grad = profile.grad_year if profile else None

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
            full_name=cand_full,
            preferred_name=cand_pref,
            firm=cand_firm,
            city=(profile.city if profile else hit.city),
            bar_card_number=(profile.bar_card_number if profile else ""),
            license_date=profile.license_date.isoformat() if (profile and profile.license_date) else None,
            license_status=(profile.license_status if profile else hit.license_status),
            law_school=(profile.law_school if profile else ""),
            grad_year=cand_grad,
            explanation="; ".join(reasons),
            profile=profile,
        )

    def _search_provider(self, name: str, firm_core: str = "", city: str = "") -> list[SearchHit]:
        """Query directory provider using either standardized protocol or colleague search."""
        if hasattr(self.client, "search_by_affiliation") and firm_core:
            dir_hits = self.client.search_by_affiliation(last_name=name, firm_core=firm_core)
        elif hasattr(self.client, "search_by_location") and city:
            dir_hits = self.client.search_by_location(name=name, city=city)
        elif hasattr(self.client, "search_statewide") and not firm_core and not city:
            dir_hits = self.client.search_statewide(last_name=name)
        elif hasattr(self.client, "search_colleague"):
            return self.client.search_colleague(name=name, company_name=firm_core, city=city)
        else:
            return []

        from txbar.parser import NameParts

        converted: list[SearchHit] = []
        for dh in dir_hits:
            if isinstance(dh, SearchHit):
                converted.append(dh)
            else:
                converted.append(
                    SearchHit(
                        contact_id=getattr(dh, "external_id", getattr(dh, "contact_id", "")),
                        name=NameParts(
                            first_name=getattr(dh, "first_name", ""),
                            middle_name=getattr(dh, "middle_name", ""),
                            last_name=getattr(dh, "last_name", ""),
                            suffix=getattr(dh, "suffix", ""),
                            preferred_name=getattr(dh, "preferred_name", ""),
                        ),
                        firm=getattr(dh, "firm", ""),
                        city=getattr(dh, "city", ""),
                        address=getattr(dh, "address", ""),
                        phone=getattr(dh, "phone", ""),
                        license_status=getattr(dh, "license_status", ""),
                    )
                )
        return converted

    def resolve_attorney(self, input_record: dict[str, Any]) -> MatchDecision:
        """Execute the multi-stage waterfall search and scoring for an attorney."""
        raw_name = input_record.get("name") or ""
        email_val = input_record.get("email") or ""
        firm_val = input_record.get("firm") or ""
        city_val = input_record.get("city") or ""
        cached_contact_id = input_record.get("txbar_contact_id")
        input_record.get("bar_card_number")

        parsed_name = parse_input_name(raw_name, email=email_val)
        email_hints = parse_email(email_val, parsed_name)

        # -------------------------------------------------------------
        # Stage 0: Direct Cache Lookup
        # -------------------------------------------------------------
        if cached_contact_id:
            try:
                prof = self.client.get_profile(str(cached_contact_id))
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
                return MatchDecision(
                    best_candidate=eval_cand,
                    candidates=[eval_cand],
                    match_score=eval_cand.score,
                    score_name=eval_cand.score_name,
                    score_firm=eval_cand.score_firm,
                    score_temporal=eval_cand.score_temporal,
                    score_geo=eval_cand.score_geo,
                    match_status="cached_match",
                    match_explanation=f"Direct cache hit via ContactID {cached_contact_id}",
                    firm_changed=False,
                    search_stage="cached_lookup",
                    review_status="auto_approved"
                    if eval_cand.score >= self.settings.match_auto_approve
                    else "pending_review",
                )
            except Exception as e:
                logger.warning(
                    "Cached contact_id %s failed: %s. Falling back to search.", cached_contact_id, e
                )

        # -------------------------------------------------------------
        # Waterfall Searches
        # -------------------------------------------------------------
        seen_contact_ids: set[str] = set()
        search_hits: list[tuple[SearchHit, str]] = []

        # Stage 1: Last name + Distinctive Firm Core (Highest precision, avoids homonym explosion)
        firm_term = firm_search_term(firm_val)
        if parsed_name.last and firm_term:
            try:
                hits = self._search_provider(name=parsed_name.last, firm_core=firm_term)
                for h in hits:
                    if h.contact_id not in seen_contact_ids:
                        seen_contact_ids.add(h.contact_id)
                        search_hits.append((h, "stage1_last_firm"))
            except Exception as e:
                logger.warning("Stage 1 search failed: %s", e)

        # Stage 2: Name + City (Standard geo targeted search)
        # If Stage 1 produced 0 or only poor hits, or we want full recall:
        if len(search_hits) == 0:
            query_name = (
                parsed_name.last
                if parsed_name.first_is_initial
                else f"{parsed_name.first} {parsed_name.last}"
            )
            try:
                hits = self._search_provider(name=query_name, city=city_val)
                for h in hits:
                    if h.contact_id not in seen_contact_ids:
                        seen_contact_ids.add(h.contact_id)
                        search_hits.append((h, "stage2_name_city"))
            except Exception as e:
                logger.warning("Stage 2 search failed: %s", e)

        # Stage 3: Statewide Last Name Relaxation (Catches lateral moves across Texas or suburban offices)
        if len(search_hits) == 0:
            for variant in parsed_name.last_variants:
                try:
                    hits = self._search_provider(name=variant)
                    for h in hits:
                        if h.contact_id not in seen_contact_ids:
                            seen_contact_ids.add(h.contact_id)
                            search_hits.append((h, f"stage3_statewide_{variant}"))
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

        # Fetch detail profiles for top N candidates to perform deep scoring
        evaluations: list[CandidateEvaluation] = []
        top_candidates = search_hits[: self.settings.scraper_max_detail_fetches]

        for rank, (hit, stage_tag) in enumerate(top_candidates, start=1):
            try:
                profile = self.client.get_profile(hit.contact_id)
            except Exception as e:
                logger.warning("Failed fetching profile for %s: %s", hit.contact_id, e)
                profile = None

            eval_cand = self.evaluate_candidate(
                hit, input_record, parsed_name, email_hints, profile, stage=stage_tag, rank=rank
            )
            evaluations.append(eval_cand)

        evaluations.sort(key=lambda c: c.score, reverse=True)
        best = evaluations[0]

        # Detect firm change
        # If candidate has high name match and geo/temporal match, but reported firm differs:
        norm_input_firm = canonical_firm(firm_val)
        norm_cand_firm = canonical_firm(best.firm)
        firm_changed = bool(
            best.score_name >= 30.0
            and norm_input_firm
            and norm_cand_firm
            and norm_input_firm != norm_cand_firm
            and best.score_firm == 0.0
        )

        # Check for ambiguity among multiple high candidates
        is_ambiguous = False
        if len(evaluations) > 1:
            second_best = evaluations[1]
            if (best.score >= self.settings.match_review_floor) and (
                abs(best.score - second_best.score) < 8.0
            ):
                is_ambiguous = True

        # Status classification
        if is_ambiguous:
            match_status = "ambiguous"
            review_status = "pending_review"
            explanation = (
                f"Ambiguous candidates: Top match score {best.score:.1f} vs runner-up {evaluations[1].score:.1f}. "
                "Sent to human review queue."
            )
        elif firm_changed and best.score >= self.settings.match_review_floor:
            match_status = "firm_change_match"
            review_status = (
                "pending_review" if best.score < self.settings.match_auto_approve else "auto_approved"
            )
            explanation = (
                f"Lateral move detected! Candidate matches identity but firm changed from "
                f"'{firm_val}' to '{best.firm}'. Confidence: {best.score:.1f}%."
            )
        elif best.score >= self.settings.match_auto_approve:
            match_status = "exact_match" if best.score_firm == 25.0 else "alias_match"
            review_status = "auto_approved"
            explanation = f"High confidence match ({best.score:.1f}%): {best.explanation}"
        elif best.score >= self.settings.match_review_floor:
            match_status = "low_confidence"
            review_status = "pending_review"
            explanation = (
                f"Moderate confidence ({best.score:.1f}%). Requires human verification: {best.explanation}"
            )
        else:
            match_status = "not_found"
            review_status = "not_found"
            explanation = f"Top candidate score ({best.score:.1f}%) below review threshold ({self.settings.match_review_floor}%)."

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
