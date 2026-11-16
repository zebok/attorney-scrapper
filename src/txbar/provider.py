"""Unified Directory Provider domain models and protocol.

Decouples the entity resolution pipeline from specific state bar directory implementations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Protocol


@dataclass
class NameParts:
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    preferred_name: str = ""
    suffix: str = ""

    @property
    def full_name(self) -> str:
        parts = [self.first_name, self.middle_name, self.last_name, self.suffix]
        return " ".join(p for p in parts if p)


@dataclass
class SearchHit:
    """Standardized search result returned by a directory provider."""

    contact_id: str
    name: NameParts
    firm: str = ""
    city: str = ""
    address: str = ""
    phone: str = ""
    license_status: str = ""
    practice_areas: list[str] = field(default_factory=list)


@dataclass
class Profile:
    """Full attorney profile returned by a directory provider."""

    contact_id: str
    name: NameParts
    license_status: str = ""
    bar_card_number: str = ""
    license_date: date | None = None
    firm: str = ""
    firm_size: str = ""
    occupation: str = ""
    practice_location: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    phone: str = ""
    practice_areas: list[str] = field(default_factory=list)
    law_school: str = ""
    law_degree: str = ""
    grad_year: int | None = None
    disciplinary_history: str = ""
    profile_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(d.pop("name"))
        d["full_name"] = self.name.full_name
        return d


@dataclass
class ProviderHealth:
    """Health check status of the remote directory provider."""

    status: str  # healthy | degraded | unhealthy
    latency_s: float
    canary_passed: bool
    http_status: int | None = None
    error: str | None = None


class DirectoryProvider(Protocol):
    """Abstract interface implemented by any state bar directory adapter."""

    provider_id: str
    provider_name: str

    def __enter__(self) -> DirectoryProvider: ...

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None: ...

    def profile_url(self, contact_id: str) -> str: ...

    def search_by_affiliation(self, last_name: str, firm_core: str) -> list[SearchHit]: ...

    def search_by_location(self, name: str, city: str) -> list[SearchHit]: ...

    def search_statewide(self, last_name: str) -> list[SearchHit]: ...

    def get_profile(self, contact_id: str) -> Profile: ...

    def health_check(self) -> ProviderHealth: ...
