"""Directory Provider Abstraction (Provider / Adapter Pattern).

Decouples the entity resolution and enrichment pipeline from specific state bar
directory websites (Texas, California, New York, Florida).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol


@dataclass
class DirectoryHit:
    """Standardized search hit returned by any state bar directory."""

    external_id: str
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    suffix: str = ""
    preferred_name: str = ""
    full_name: str = ""
    firm: str = ""
    city: str = ""
    address: str = ""
    phone: str = ""
    license_status: str = ""
    practice_areas: list[str] = field(default_factory=list)


@dataclass
class DirectoryProfile:
    """Comprehensive attorney public profile returned by any state bar directory."""

    external_id: str
    bar_card_number: str
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    suffix: str = ""
    preferred_name: str = ""
    full_name: str = ""
    license_status: str = ""
    license_date: date | None = None
    current_firm: str = ""
    firm_size: str = ""
    occupation: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    phone: str = ""
    practice_areas: list[str] = field(default_factory=list)
    law_school: str = ""
    grad_year: int | None = None
    profile_url: str = ""


@dataclass
class ProviderHealth:
    """Health check status of the remote directory provider."""

    status: str  # healthy | degraded | unhealthy
    latency_s: float
    canary_passed: bool
    details: dict[str, Any] = field(default_factory=dict)


class DirectoryProvider(Protocol):
    """Abstract interface that any state legal directory adapter must implement."""

    @property
    def provider_id(self) -> str:
        """Unique key identifying the directory (e.g. 'tx_bar', 'ca_bar', 'ny_bar')."""
        ...

    @property
    def provider_name(self) -> str:
        """Human-readable display name."""
        ...

    def search_by_affiliation(self, last_name: str, firm_core: str) -> list[DirectoryHit]:
        """Search directory filtering by attorney surname and firm/organization core."""
        ...

    def search_by_location(self, name: str, city: str) -> list[DirectoryHit]:
        """Search directory filtering by attorney name and city/metro jurisdiction."""
        ...

    def search_statewide(self, last_name: str) -> list[DirectoryHit]:
        """Broad statewide search by surname when targeted criteria return 0 hits."""
        ...

    def get_profile(self, external_id: str) -> DirectoryProfile:
        """Fetch and parse full profile details for an attorney by their directory ID."""
        ...

    def health_check(self) -> ProviderHealth:
        """Probe remote directory availability and assert DOM canary integrity."""
        ...
