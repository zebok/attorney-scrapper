"""Data-driven Domain Knowledge Base.

Loads declarative business rules and mappings from external JSON files:
- Legal nickname equivalence clusters
- Law firm mergers, rebrandings, and alias mappings
- Corporate email domain to firm identity mappings
- Metropolitan statistical areas / city clusters

Allows updating domain knowledge without touching or redeploying code.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES_DIR = PROJECT_ROOT / "data" / "rules"


class KnowledgeBase:
    """Manages domain dictionaries, aliases, and geographic mappings."""

    def __init__(self, rules_dir: Path | None = None) -> None:
        self.rules_dir = rules_dir or DEFAULT_RULES_DIR
        self._nicknames: dict[str, set[str]] = {}
        self._firm_aliases: dict[str, set[str]] = {}
        self._firm_domains: dict[str, str] = {}
        self._city_to_metro: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        """Load or reload all declarative rule files from disk."""
        self._load_nicknames()
        self._load_firm_aliases()
        self._load_firm_domains()
        self._load_metro_areas()

    def _load_nicknames(self) -> None:
        file_path = self.rules_dir / "nicknames.json"
        self._nicknames.clear()
        if not file_path.exists():
            return
        try:
            groups = json.loads(file_path.read_text(encoding="utf-8"))
            for group in groups:
                s_group = {str(n).lower().strip() for n in group}
                for name in s_group:
                    self._nicknames.setdefault(name, set()).update(s_group)
            logger.debug("Loaded %d nickname mappings from %s", len(self._nicknames), file_path)
        except Exception as e:
            logger.warning("Failed loading %s: %s", file_path, e)

    def _load_firm_aliases(self) -> None:
        file_path = self.rules_dir / "firm_aliases.json"
        self._firm_aliases.clear()
        if not file_path.exists():
            return
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
            for canon, aliases in raw.items():
                s_aliases = {str(a).lower().strip() for a in aliases}
                self._firm_aliases[canon.lower().strip()] = s_aliases
            logger.debug("Loaded %d firm alias mappings from %s", len(self._firm_aliases), file_path)
        except Exception as e:
            logger.warning("Failed loading %s: %s", file_path, e)

    def _load_firm_domains(self) -> None:
        file_path = self.rules_dir / "firm_domains.json"
        self._firm_domains.clear()
        if not file_path.exists():
            return
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
            self._firm_domains = {k.lower().strip(): v.lower().strip() for k, v in raw.items()}
            logger.debug("Loaded %d firm domain mappings from %s", len(self._firm_domains), file_path)
        except Exception as e:
            logger.warning("Failed loading %s: %s", file_path, e)

    def _load_metro_areas(self) -> None:
        file_path = self.rules_dir / "metro_areas.json"
        self._city_to_metro.clear()
        if file_path.exists():
            try:
                raw = json.loads(file_path.read_text(encoding="utf-8"))
                for metro, cities in raw.items():
                    for city in cities:
                        self._city_to_metro[city.lower().strip()] = metro.lower().strip()
                logger.debug("Loaded %d city-metro mappings from %s", len(self._city_to_metro), file_path)
                return
            except Exception as e:
                logger.warning("Failed loading %s: %s", file_path, e)

    # Public Query API
    def are_nicknames(self, name_a: str, name_b: str) -> bool:
        """Check if two first names belong to the same nickname equivalence group."""
        na, nb = name_a.lower().strip(), name_b.lower().strip()
        if na == nb:
            return True
        return nb in self._nicknames.get(na, set())

    def get_nicknames(self, name: str) -> set[str]:
        """Return all known nicknames and formal variants for a given name."""
        return self._nicknames.get(name.lower().strip(), set())

    def resolve_firm_alias(self, firm_candidate: str) -> str:
        """Return the canonical firm key if an alias match is found, else unchanged."""
        f_norm = firm_candidate.lower().strip()
        for canon, aliases in self._firm_aliases.items():
            if f_norm == canon or f_norm in aliases:
                return canon
        return f_norm

    def are_firm_aliases(self, firm_a: str, firm_b: str) -> bool:
        """Check if two firm names resolve to the same canonical firm entity."""
        fa = self.resolve_firm_alias(firm_a)
        fb = self.resolve_firm_alias(firm_b)
        if fa == fb:
            return True
        # Check cross-alias sets
        aliases_a = self._firm_aliases.get(fa, set())
        aliases_b = self._firm_aliases.get(fb, set())
        return bool(fa in aliases_b or fb in aliases_a or aliases_a.intersection(aliases_b))

    def get_firm_from_domain(self, domain: str) -> str | None:
        """Identify canonical firm associated with an email domain."""
        return self._firm_domains.get(domain.lower().strip())

    def get_metro(self, city: str) -> str | None:
        """Return metropolitan area identifier for a given city."""
        return self._city_to_metro.get(city.lower().strip())

    def same_metro(self, city_a: str, city_b: str) -> bool:
        """Check if two cities belong to the same metropolitan cluster."""
        ca, cb = city_a.lower().strip(), city_b.lower().strip()
        if ca == cb and ca:
            return True
        ma = self.get_metro(ca)
        mb = self.get_metro(cb)
        return bool(ma and mb and ma == mb)

    @property
    def nicknames_map(self) -> dict[str, set[str]]:
        return self._nicknames

    @property
    def firm_aliases_map(self) -> dict[str, set[str]]:
        return self._firm_aliases

    @property
    def firm_domains_map(self) -> dict[str, str]:
        return self._firm_domains

    @property
    def metro_map(self) -> dict[str, str]:
        return self._city_to_metro


@lru_cache(maxsize=1)
def get_knowledge_base() -> KnowledgeBase:
    """Singleton provider for KnowledgeBase."""
    return KnowledgeBase()
