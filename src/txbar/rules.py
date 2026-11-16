"""Data-driven Domain Knowledge Base.

Loads declarative mappings from JSON files (nicknames, firm aliases, corporate email
domains, metropolitan clusters) configured via Settings.rules_dir.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from txbar.config import get_settings

logger = logging.getLogger(__name__)


def _norm(s: str | None) -> str:
    if not s:
        return ""
    stripped = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped.lower()).strip()


class KnowledgeBase:
    """Manages domain dictionaries, aliases, and geographic mappings."""

    def __init__(self, rules_dir: Path | None = None) -> None:
        settings = get_settings()
        self.rules_dir = rules_dir or settings.resolve(settings.rules_dir)
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
                s_group = {_norm(str(n)) for n in group if _norm(str(n))}
                for name in s_group:
                    self._nicknames.setdefault(name, set()).update(s_group)
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
                self._firm_aliases[_norm(canon)] = {_norm(str(a)) for a in aliases if _norm(str(a))}
        except Exception as e:
            logger.warning("Failed loading %s: %s", file_path, e)

    def _load_firm_domains(self) -> None:
        file_path = self.rules_dir / "firm_domains.json"
        self._firm_domains.clear()
        if not file_path.exists():
            return
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
            self._firm_domains = {_norm(k): _norm(v) for k, v in raw.items()}
        except Exception as e:
            logger.warning("Failed loading %s: %s", file_path, e)

    def _load_metro_areas(self) -> None:
        file_path = self.rules_dir / "metro_areas.json"
        self._city_to_metro.clear()
        if not file_path.exists():
            return
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
            for metro, cities in raw.items():
                m_key = _norm(metro)
                for city in cities:
                    self._city_to_metro[_norm(city)] = m_key
        except Exception as e:
            logger.warning("Failed loading %s: %s", file_path, e)

    def are_nicknames(self, name_a: str, name_b: str) -> bool:
        na, nb = _norm(name_a), _norm(name_b)
        if not na or not nb:
            return False
        return na == nb or nb in self._nicknames.get(na, set())

    def get_nicknames(self, name: str) -> set[str]:
        return self._nicknames.get(_norm(name), set())

    def resolve_firm_alias(self, firm_candidate: str) -> str:
        f_norm = _norm(firm_candidate)
        for canon, aliases in self._firm_aliases.items():
            if f_norm == canon or f_norm in aliases:
                return canon
        return f_norm

    def are_firm_aliases(self, firm_a: str, firm_b: str) -> bool:
        fa = self.resolve_firm_alias(firm_a)
        fb = self.resolve_firm_alias(firm_b)
        if fa == fb:
            return True
        aliases_a = self._firm_aliases.get(fa, set())
        aliases_b = self._firm_aliases.get(fb, set())
        return bool(fa in aliases_b or fb in aliases_a or aliases_a.intersection(aliases_b))

    def get_firm_from_domain(self, domain: str) -> str | None:
        return self._firm_domains.get(_norm(domain))

    def get_metro(self, city: str) -> str | None:
        return self._city_to_metro.get(_norm(city))

    def same_metro(self, city_a: str, city_b: str) -> bool:
        ca, cb = _norm(city_a), _norm(city_b)
        if ca == cb and ca:
            return True
        ma = self.get_metro(ca)
        mb = self.get_metro(cb)
        return bool(ma and mb and ma == mb)


@lru_cache(maxsize=1)
def get_knowledge_base() -> KnowledgeBase:
    """Default singleton KnowledgeBase resolved from Settings.rules_dir."""
    return KnowledgeBase()
