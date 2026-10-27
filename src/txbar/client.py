"""Polite, resilient HTTP client for the Texas Bar directory.

Features:
- Polite rate-limiting with random jitter (exceeding robots.txt Crawl-delay: 1).
- Automatic session bootstrapping (ColdFusion CFID / CFTOKEN / JSESSIONID cookies).
- Exponential backoff with jitter for transient 5xx, 429, or network timeouts.
- DOM canary checks on every request to detect site outages, maintenance, or layout drift early.
- Metrics tracking (request count, latency, retries) for run-level observability.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from txbar.config import Settings, get_settings
from txbar.parser import DomDriftError, Profile, SearchHit, parse_profile, parse_search_results
from txbar.provider import DirectoryHit, DirectoryProfile, ProviderHealth

logger = logging.getLogger(__name__)

BASE_URL = "https://www.texasbar.com/AM/Template.cfm?Section=Find_A_Lawyer&Template="
SEARCH_FORM_URL = BASE_URL + "/CustomSource/MemberDirectory/Search_Form_Client_Main.cfm"
SEARCH_RESULTS_URL = BASE_URL + "/CustomSource/MemberDirectory/Result_form_client.cfm"
DETAIL_URL = BASE_URL + "/CustomSource/MemberDirectory/MemberDirectoryDetail.cfm&ContactID="


@dataclass
class ClientMetrics:
    total_requests: int = 0
    retries_count: int = 0
    total_latency_s: float = 0.0


def hit_to_directory_hit(hit: SearchHit) -> DirectoryHit:
    return DirectoryHit(
        external_id=hit.contact_id,
        first_name=hit.name.first_name,
        middle_name=hit.name.middle_name,
        last_name=hit.name.last_name,
        suffix=hit.name.suffix,
        preferred_name=hit.name.preferred_name,
        full_name=hit.name.full_name,
        firm=hit.firm,
        city=hit.city,
        address=hit.address,
        phone=hit.phone,
        license_status=hit.license_status,
        practice_areas=hit.practice_areas,
    )


def profile_to_directory_profile(p: Profile) -> DirectoryProfile:
    return DirectoryProfile(
        external_id=p.contact_id,
        bar_card_number=p.bar_card_number,
        first_name=p.name.first_name,
        middle_name=p.name.middle_name,
        last_name=p.name.last_name,
        suffix=p.name.suffix,
        preferred_name=p.name.preferred_name,
        full_name=p.name.full_name,
        license_status=p.license_status,
        license_date=p.license_date,
        current_firm=p.firm,
        firm_size=p.firm_size,
        occupation=p.occupation,
        address=p.address,
        city=p.city,
        state=p.state,
        zip_code=p.zip,
        phone=p.phone,
        practice_areas=p.practice_areas,
        law_school=p.law_school,
        grad_year=p.grad_year,
        profile_url=p.profile_url,
    )


class TexasBarClient:
    provider_id: str = "tx_bar"
    provider_name: str = "State Bar of Texas"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = httpx.Client(
            timeout=self.settings.scraper_timeout_s,
            follow_redirects=True,
            headers={
                "User-Agent": self.settings.scraper_user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        self.metrics = ClientMetrics()
        self._session_initialized = False
        self._requests_since_bootstrap = 0
        self._last_request_time = 0.0

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> TexasBarClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _wait_polite(self) -> None:
        """Enforce polite rate limiting with random jitter between requests."""
        if self._last_request_time > 0:
            elapsed = time.time() - self._last_request_time
            delay = random.uniform(self.settings.scraper_min_delay_s, self.settings.scraper_max_delay_s)
            if elapsed < delay:
                time.sleep(delay - elapsed)

    def _send_request(self, method: str, url: str, data: dict[str, Any] | None = None) -> httpx.Response:
        retries = 0
        backoff = 2.0
        while True:
            self._wait_polite()
            t0 = time.time()
            try:
                self.metrics.total_requests += 1
                resp = self.client.get(url) if method == "GET" else self.client.post(url, data=data)
                self.metrics.total_latency_s += time.time() - t0
                self._last_request_time = time.time()

                if resp.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError("Transient server error", request=resp.request, response=resp)

                resp.raise_for_status()
                return resp
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                retries += 1
                self.metrics.retries_count += 1
                if retries > self.settings.scraper_max_retries:
                    logger.error("Max retries exceeded for %s: %s", url, exc)
                    raise
                jitter = random.uniform(0.5, 1.5)
                sleep_time = (backoff**retries) + jitter
                logger.warning(
                    "Request error (%s) on %s. Retry %d/%d after %.2fs...",
                    exc,
                    url,
                    retries,
                    self.settings.scraper_max_retries,
                    sleep_time,
                )
                time.sleep(sleep_time)

    def bootstrap_session(self, force: bool = False) -> None:
        """Visit directory main search form once to obtain session cookies and verify site health."""
        if self._session_initialized and not force:
            return
        logger.info("Bootstrapping Texas Bar directory session (force=%s)...", force)
        if force:
            self.client.cookies.clear()
        resp = self._send_request("GET", SEARCH_FORM_URL)
        if "Find A Lawyer" not in resp.text and "SearchForm_Colleague" not in resp.text:
            raise DomDriftError("Directory search landing page structure mismatch.")
        self._session_initialized = True
        self._requests_since_bootstrap = 0
        logger.info("Texas Bar session cookies acquired successfully.")

    def search_colleague(
        self,
        name: str = "",
        company_name: str = "",
        city: str = "",
        bar_card_number: str = "",
    ) -> list[SearchHit]:
        """Perform a colleague search with the given parameters and parse the resulting hits."""
        self.bootstrap_session()
        payload = {
            "Name": name.strip(),
            "CompanyName": company_name.strip(),
            "BarCardNumber": bar_card_number.strip(),
            "PPlCityName": city.strip(),
            "County": "",
            "State": "",
            "Zip": "",
            "Submitted": "1",
            "ShowPrinter": "1",
            "Find": "1",
        }
        resp = self._send_request("POST", SEARCH_RESULTS_URL, data=payload)
        self._requests_since_bootstrap += 1
        try:
            hits, _ = parse_search_results(resp.text)
            return hits
        except DomDriftError:
            logger.warning("DomDriftError parsing search results. Refreshing session and retrying...")
            self.bootstrap_session(force=True)
            retry_resp = self._send_request("POST", SEARCH_RESULTS_URL, data=payload)
            hits, _ = parse_search_results(retry_resp.text)
            return hits

    def get_profile(self, contact_id: str) -> Profile:
        """Fetch the detail page for a contact_id and parse the profile."""
        self.bootstrap_session()
        url = f"{DETAIL_URL}{contact_id}"
        resp = self._send_request("GET", url)
        self._requests_since_bootstrap += 1
        try:
            return parse_profile(resp.text, contact_id)
        except DomDriftError:
            # ColdFusion session timed out or detail page returned search landing page.
            logger.warning(
                "DomDriftError parsing profile for ContactID %s. Forcing session bootstrap and retrying...",
                contact_id,
            )
            self.bootstrap_session(force=True)
            retry_resp = self._send_request("GET", url)
            return parse_profile(retry_resp.text, contact_id)

    # -------------------------------------------------------------
    # DirectoryProvider Protocol Implementation
    # -------------------------------------------------------------
    def search_by_affiliation(self, last_name: str, firm_core: str) -> list[DirectoryHit]:
        """Search directory filtering by attorney surname and firm/organization core."""
        hits = self.search_colleague(name=last_name, company_name=firm_core)
        return [hit_to_directory_hit(h) for h in hits]

    def search_by_location(self, name: str, city: str) -> list[DirectoryHit]:
        """Search directory filtering by attorney name and city/metro jurisdiction."""
        hits = self.search_colleague(name=name, city=city)
        return [hit_to_directory_hit(h) for h in hits]

    def search_statewide(self, last_name: str) -> list[DirectoryHit]:
        """Broad statewide search by surname when targeted criteria return 0 hits."""
        hits = self.search_colleague(name=last_name)
        return [hit_to_directory_hit(h) for h in hits]

    def get_directory_profile(self, external_id: str) -> DirectoryProfile:
        """Fetch and parse full profile details as a DirectoryProfile."""
        prof = self.get_profile(external_id)
        return profile_to_directory_profile(prof)

    def check_health(self) -> ProviderHealth:
        """Probe remote directory availability returning a structured ProviderHealth."""
        res = self.health_check()
        return ProviderHealth(
            status=res.get("status", "unhealthy"),
            latency_s=res.get("latency_s", 0.0),
            canary_passed=res.get("canary_passed", False),
            details=res,
        )

    def health_check(self) -> dict[str, Any]:
        """Fast health check to verify site connectivity and DOM structure."""
        t0 = time.time()
        try:
            resp = self._send_request("GET", SEARCH_FORM_URL)
            latency = time.time() - t0
            is_healthy = "SearchForm_Colleague" in resp.text and resp.status_code == 200
            return {
                "status": "healthy" if is_healthy else "degraded",
                "latency_s": round(latency, 3),
                "http_status": resp.status_code,
                "canary_passed": is_healthy,
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
                "latency_s": round(time.time() - t0, 3),
                "canary_passed": False,
            }
