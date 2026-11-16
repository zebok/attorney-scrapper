"""Polite, resilient HTTP client for the Texas Bar directory.

Implements DirectoryProvider directly with rate-limiting jitter, ColdFusion session
bootstrapping, exponential backoff retries, and DOM canary validation.
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
from txbar.provider import ProviderHealth

logger = logging.getLogger(__name__)


@dataclass
class ClientMetrics:
    total_requests: int = 0
    retries_count: int = 0
    total_latency_s: float = 0.0


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
        self._last_request_time = 0.0

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> TexasBarClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def profile_url(self, contact_id: str) -> str:
        return self.settings.detail_url(contact_id)

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
        """Visit directory search form once to obtain ColdFusion session cookies."""
        if self._session_initialized and not force:
            return
        logger.info("Bootstrapping Texas Bar directory session (force=%s)...", force)
        if force:
            self.client.cookies.clear()
        resp = self._send_request("GET", self.settings.search_form_url)
        if "Find A Lawyer" not in resp.text and "SearchForm_Colleague" not in resp.text:
            raise DomDriftError("Directory search landing page structure mismatch.")
        self._session_initialized = True

    def search_colleague(
        self,
        name: str = "",
        company_name: str = "",
        city: str = "",
        bar_card_number: str = "",
    ) -> list[SearchHit]:
        """Perform a colleague search and parse resulting hits, retrying once on session expiry."""
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
        resp = self._send_request("POST", self.settings.search_results_url, data=payload)
        try:
            hits, _ = parse_search_results(resp.text)
            return hits
        except DomDriftError:
            logger.warning("DomDriftError parsing search results. Refreshing session and retrying...")
            self.bootstrap_session(force=True)
            retry_resp = self._send_request("POST", self.settings.search_results_url, data=payload)
            hits, _ = parse_search_results(retry_resp.text)
            return hits

    # ------------------------------------------------------------------
    # DirectoryProvider Protocol
    # ------------------------------------------------------------------
    def search_by_affiliation(self, last_name: str, firm_core: str) -> list[SearchHit]:
        return self.search_colleague(name=last_name, company_name=firm_core)

    def search_by_location(self, name: str, city: str) -> list[SearchHit]:
        return self.search_colleague(name=name, city=city)

    def search_statewide(self, last_name: str) -> list[SearchHit]:
        return self.search_colleague(name=last_name)

    def get_profile(self, contact_id: str) -> Profile:
        self.bootstrap_session()
        url = self.profile_url(contact_id)
        resp = self._send_request("GET", url)
        try:
            return parse_profile(resp.text, contact_id, profile_url=url)
        except DomDriftError:
            logger.warning(
                "DomDriftError parsing profile for ContactID %s. Refreshing session and retrying...",
                contact_id,
            )
            self.bootstrap_session(force=True)
            retry_resp = self._send_request("GET", url)
            return parse_profile(retry_resp.text, contact_id, profile_url=url)

    def health_check(self) -> ProviderHealth:
        t0 = time.time()
        try:
            resp = self._send_request("GET", self.settings.search_form_url)
            latency = round(time.time() - t0, 3)
            canary_passed = "SearchForm_Colleague" in resp.text and resp.status_code == 200
            return ProviderHealth(
                status="healthy" if canary_passed else "degraded",
                latency_s=latency,
                canary_passed=canary_passed,
                http_status=resp.status_code,
            )
        except Exception as exc:
            return ProviderHealth(
                status="unhealthy",
                latency_s=round(time.time() - t0, 3),
                canary_passed=False,
                error=str(exc),
            )
