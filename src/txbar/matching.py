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

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from txbar.client import TexasBarClient
from txbar.config import Settings, get_settings
from txbar.normalize import (
    DOMAIN_FIRMS,
    EmailHints,
    ParsedName,
    alpha_key,
    canonical_firm,
    firm_key,
    firm_search_term,
    given_names_compatible,
    metro_of,
    parse_email,
    parse_input_name,
)
from txbar.parser import Profile, SearchHit

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
    def __init__(self, client: TexasBarClient, settings: Settings | None = None) -> None:
        self.client = client
        self.settings = settings or get_settings()

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
        """Score an individual candidate profile against the input record."""
        reasons: list[str] = []

        # Candidate name pieces
        cand_first = (profile.name.first_name if profile else hit.name.first_name).strip()
        cand_middle = (profile.name.middle_name if profile else hit.name.middle_name).strip()
        cand_last = (profile.name.last_name if profile else hit.name.last_name).strip()
        cand_pref = (profile.name.preferred_name if profile else hit.name.preferred_name).strip()
        cand_full = profile.name.full_name if profile else hit.name.full_name

        # -------------------------------------------------------------
        # 1. NAME SIMILARITY (Max 35)
        # -------------------------------------------------------------
        name_score = 0.0
        # Check last name first
        last_match = False
        for v in parsed_name.last_variants:
            if alpha_key(v) == alpha_key(cand_last) or JaroWinkler.similarity(alpha_key(v), alpha_key(cand_last)) > 0.92:
                last_match = True
                break

        if not last_match:
            # Maybe last name is compound in the candidate (e.g. Rivera-Alvarado)
            if any(alpha_key(v) in alpha_key(cand_last) for v in parsed_name.last_variants):
                last_match = True

        first_match = False
        first_detail = ""
        # 1a. Direct given name or nickname match
        if given_names_compatible(parsed_name.first, cand_first):
            first_match = True
            first_detail = "First name / nickname match"
        # 1b. Preferred name match (e.g. Jack for Wayne Zeringue, Bryant for William Bryant Buechele)
        elif cand_pref and given_names_compatible(parsed_name.first, cand_pref):
            first_match = True
            first_detail = f"Matched preferred name '{cand_pref}'"
        # 1c. Middle name match if input first was initial or second name
        elif cand_middle and given_names_compatible(parsed_name.first, cand_middle):
            first_match = True
            first_detail = f"Matched middle name '{cand_middle}'"
        # 1d. Email hints disambiguation
        elif email_hints and email_hints.given_token and (
            given_names_compatible(email_hints.given_token, cand_first)
            or (cand_pref and given_names_compatible(email_hints.given_token, cand_pref))
            or (cand_middle and given_names_compatible(email_hints.given_token, cand_middle))
        ):
            first_match = True
            first_detail = f"Matched email name prefix '{email_hints.given_token}'"

        if last_match and first_match:
            name_score = 35.0
            reasons.append(f"Full name matched ({first_detail})")
        elif last_match:
            # Check if email middle initial matches
            if email_hints and email_hints.middle_initial and cand_middle:
                if alpha_key(cand_middle).startswith(email_hints.middle_initial):
                    name_score = 30.0
                    reasons.append(f"Family name matched + email middle initial '{email_hints.middle_initial}'")
                else:
                    name_score = 15.0
                    reasons.append("Family name matched but first/middle name differs")
            else:
                sim = JaroWinkler.similarity(alpha_key(parsed_name.first), alpha_key(cand_first))
                name_score = 15.0 + (sim * 15.0)
                reasons.append(f"Family name matched, first name partial similarity ({sim:.2f})")
        else:
            sim = JaroWinkler.similarity(alpha_key(parsed_name.raw), alpha_key(cand_full))
            name_score = sim * 25.0
            reasons.append(f"Approximate name similarity ({sim:.2f})")

        # -------------------------------------------------------------
        # 2. FIRM SIMILARITY (Max 25)
        # -------------------------------------------------------------
        firm_score = 0.0
        input_firm = input_record.get("firm") or ""
        cand_firm = (profile.firm if profile else hit.firm).strip()
        norm_input_firm = canonical_firm(input_firm)
        norm_cand_firm = canonical_firm(cand_firm)

        # Also check email domain mapping
        email_val = input_record.get("email") or ""
        domain = email_val.split("@")[-1].lower() if "@" in email_val else ""
        domain_canon = DOMAIN_FIRMS.get(domain, "")

        firm_matched = False
        if norm_input_firm and norm_cand_firm and norm_input_firm == norm_cand_firm:
            firm_score = 25.0
            firm_matched = True
            reasons.append(f"Firm exact match: '{cand_firm}'")
        elif domain_canon and norm_cand_firm and (domain_canon == norm_cand_firm or domain_canon in norm_cand_firm):
            firm_score = 25.0
            firm_matched = True
            reasons.append(f"Firm matched via email domain alias ({domain} -> '{cand_firm}')")
        elif norm_input_firm and norm_cand_firm:
            fuzz_ratio = fuzz.token_sort_ratio(firm_key(input_firm), firm_key(cand_firm))
            if fuzz_ratio >= 80:
                firm_score = 22.0
                firm_matched = True
                reasons.append(f"Firm fuzzy match ({fuzz_ratio}%): '{cand_firm}'")
            elif fuzz_ratio >= 50:
                firm_score = 12.0
                reasons.append(f"Firm partial overlap ({fuzz_ratio}%): '{cand_firm}'")
            else:
                firm_score = 0.0
                reasons.append(f"Different firm reported: '{cand_firm}' vs '{input_firm}'")
        elif cand_firm:
            firm_score = 5.0
            reasons.append(f"Candidate firm present: '{cand_firm}'")

        # -------------------------------------------------------------
        # 3. TEMPORAL CONSISTENCY (Max 25)
        # -------------------------------------------------------------
        temp_score = 0.0
        input_jd = input_record.get("jd_year")
        cand_grad = profile.grad_year if profile else None
        cand_lic_year = profile.license_date.year if (profile and profile.license_date) else None

        # Compare with grad year if available, else license year
        ref_year = cand_grad or cand_lic_year

        if input_jd and ref_year:
            diff = abs(int(input_jd) - int(ref_year))
            if diff == 0:
                temp_score = 25.0
                reasons.append(f"Exact graduation/admission year match ({ref_year})")
            elif diff == 1:
                temp_score = 22.0
                reasons.append(f"Close graduation/admission year (+-{diff} year: {ref_year} vs {input_jd})")
            elif diff <= 2:
                temp_score = 16.0
                reasons.append(f"Acceptable graduation/admission year delta (+-{diff} years)")
            elif diff <= 4:
                temp_score = 8.0
                reasons.append(f"Moderate graduation delta (+-{diff} years)")
            else:
                # If input_jd is 2017 (the suspicious batch default), don't penalize harshly if firm and name match
                if int(input_jd) == 2017 and (name_score >= 30 and firm_matched):
                    temp_score = 12.0
                    reasons.append(f"Year delta {diff} discounted (input 2017 batch anomaly; high name & firm match)")
                else:
                    temp_score = 0.0
                    reasons.append(f"Large graduation/admission gap ({diff} years: {ref_year} vs {input_jd})")
        else:
            # No temporal info to compare
            temp_score = 12.5  # neutral baseline
            reasons.append("Temporal data partially missing (awarded neutral score)")

        # -------------------------------------------------------------
        # 4. GEOGRAPHIC MATCH (Max 15)
        # -------------------------------------------------------------
        geo_score = 0.0
        input_city = (input_record.get("city") or "").strip().lower()
        cand_city = ((profile.city if profile else hit.city) or "").strip().lower()

        if input_city and cand_city:
            if input_city == cand_city:
                geo_score = 15.0
                reasons.append(f"Same city: '{cand_city.title()}'")
            elif metro_of(input_city) == metro_of(cand_city):
                geo_score = 12.0
                reasons.append(f"Same metro area ({metro_of(input_city).upper()}): '{cand_city.title()}'")
            else:
                geo_score = 5.0
                reasons.append(f"Different Texas city: '{cand_city.title()}' vs '{input_city.title()}'")
        else:
            geo_score = 8.0

        total_score = min(100.0, max(0.0, name_score + firm_score + temp_score + geo_score))

        return CandidateEvaluation(
            contact_id=hit.contact_id,
            search_stage=stage,
            rank=rank,
            score=round(total_score, 1),
            score_name=round(name_score, 1),
            score_firm=round(firm_score, 1),
            score_temporal=round(temp_score, 1),
            score_geo=round(geo_score, 1),
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
                    review_status="auto_approved" if eval_cand.score >= self.settings.match_auto_approve else "pending_review",
                )
            except Exception as e:
                logger.warning("Cached contact_id %s failed: %s. Falling back to search.", cached_contact_id, e)

        # -------------------------------------------------------------
        # Waterfall Searches
        # -------------------------------------------------------------
        seen_contact_ids: set[str] = set()
        search_hits: list[tuple[SearchHit, str]] = []

        # Stage 1: Last name + Distinctive Firm Core (Highest precision, avoids homonym explosion)
        firm_term = firm_search_term(firm_val)
        if parsed_name.last and firm_term:
            try:
                hits = self.client.search_colleague(name=parsed_name.last, company_name=firm_term)
                for h in hits:
                    if h.contact_id not in seen_contact_ids:
                        seen_contact_ids.add(h.contact_id)
                        search_hits.append((h, "stage1_last_firm"))
            except Exception as e:
                logger.warning("Stage 1 search failed: %s", e)

        # Stage 2: Name + City (Standard geo targeted search)
        # If Stage 1 produced 0 or only poor hits, or we want full recall:
        if len(search_hits) == 0:
            query_name = parsed_name.last if parsed_name.first_is_initial else f"{parsed_name.first} {parsed_name.last}"
            try:
                hits = self.client.search_colleague(name=query_name, city=city_val)
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
                    hits = self.client.search_colleague(name=variant)
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
            review_status = "pending_review" if best.score < self.settings.match_auto_approve else "auto_approved"
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
            explanation = f"Moderate confidence ({best.score:.1f}%). Requires human verification: {best.explanation}"
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
