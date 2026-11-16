"""Name, email, firm, geography, and phone normalization utilities."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from txbar.rules import KnowledgeBase, get_knowledge_base

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "esq"}

_LEGAL_SUFFIX_TOKENS = {"llp", "pllc", "pc", "llc", "lp", "pa", "ltd", "inc", "plc", "p", "c", "l"}
_FIRM_STOPWORDS = {"the", "and", "of", "law", "firm", "group", "office", "offices", "attorneys", "at"}


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def alpha_key(s: str | None) -> str:
    """Lowercase ASCII letters only. "O'Brien" -> "obrien", "Alleluia-Feinberg" -> "alleluiafeinberg"."""
    if not s:
        return ""
    return re.sub(r"[^a-z]", "", strip_accents(s).lower())


def tokens(s: str | None) -> list[str]:
    if not s:
        return []
    return [t for t in re.split(r"[\s]+", strip_accents(s).strip()) if t]


@dataclass
class ParsedName:
    raw: str
    first: str
    middle: str
    last: str
    suffix: str
    first_is_initial: bool
    last_variants: list[str] = field(default_factory=list)

    @property
    def first_key(self) -> str:
        return alpha_key(self.first)

    @property
    def last_key(self) -> str:
        return alpha_key(self.last)


def _is_initial(tok: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]\.?", tok))


def parse_input_name(raw: str, email: str | None = None) -> ParsedName:
    """Parse a free-form attorney name, using email local-part to disambiguate compound surnames."""
    toks = tokens(raw.replace(",", " "))
    suffix = ""
    while toks and alpha_key(toks[-1]) in SUFFIXES:
        suffix = toks.pop().strip(".")

    # Split glued initials: "J.W." -> ["J.", "W."]
    expanded: list[str] = []
    for t in toks:
        if re.fullmatch(r"(?:[A-Za-z]\.){2,}", t):
            expanded.extend(f"{c}." for c in t.replace(".", ""))
        else:
            expanded.append(t)
    toks = expanded

    if not toks:
        return ParsedName(raw, "", "", "", suffix, False, [])
    if len(toks) == 1:
        return ParsedName(raw, "", "", toks[0], suffix, False, [toks[0]])

    first, rest = toks[0], toks[1:]
    first_is_initial = _is_initial(first)
    initials = [t for t in rest[:-1] if _is_initial(t)]
    non_initial_rest = [t for t in rest if not _is_initial(t)]

    middle = " ".join(t.strip(".") for t in initials)
    last_tokens = non_initial_rest or rest[-1:]

    local = alpha_key((email or "").split("@")[0])
    if len(last_tokens) >= 2:
        compound = " ".join(last_tokens)
        tail = last_tokens[-1]
        head = last_tokens[0]
        if local and alpha_key(compound) in local:
            last = compound
            variants = [compound, tail, head]
        else:
            last = tail
            middle = " ".join([*(t for t in last_tokens[:-1]), middle]).strip()
            variants = [tail, compound]
    else:
        last = last_tokens[0]
        variants = [last]
        if "-" in last:
            variants += [last.replace("-", " "), last.split("-")[0]]

    return ParsedName(
        raw=raw,
        first=first.strip("."),
        middle=middle,
        last=last,
        suffix=suffix,
        first_is_initial=first_is_initial,
        last_variants=list(dict.fromkeys(variants)),
    )


@dataclass
class EmailHints:
    local: str
    domain: str
    given_token: str
    initials: str
    pattern: str

    @property
    def first_initial(self) -> str:
        return (self.initials or self.given_token)[:1]

    @property
    def middle_initial(self) -> str:
        return self.initials[1:2] if len(self.initials) >= 2 else ""


def parse_email(email: str | None, name: ParsedName) -> EmailHints | None:
    if not email or "@" not in email:
        return None
    local, domain = email.lower().strip().split("@", 1)
    local_alpha = alpha_key(local)
    last_keys = [alpha_key(v) for v in name.last_variants if alpha_key(v)]

    for lk in sorted(last_keys, key=len, reverse=True):
        idx = local_alpha.rfind(lk)
        if idx < 0:
            continue
        prefix = local_alpha[:idx]
        if not prefix:
            return EmailHints(local, domain, "", "", "last_only")
        if len(prefix) <= 2:
            return EmailHints(local, domain, "", prefix, "initials+last")
        sep = "first.last" if "." in local else "firstlast"
        return EmailHints(local, domain, prefix, "", sep)

    fk = name.first_key
    if fk and len(fk) > 1 and local_alpha.startswith(fk):
        return EmailHints(local, domain, fk, "", "first_only")
    if len(local_alpha) <= 3:
        return EmailHints(local, domain, "", local_alpha, "initials_only")
    return EmailHints(local, domain, "", "", "unknown")


def given_names_compatible(a: str, b: str, kb: KnowledgeBase | None = None) -> bool:
    """True when two given names are equal, nickname-equivalent, or one is an initial of the other."""
    ka, kb_key = alpha_key(a), alpha_key(b)
    if not ka or not kb_key:
        return False
    if ka == kb_key:
        return True
    if len(ka) == 1 or len(kb_key) == 1:
        return ka[0] == kb_key[0]
    knowledge = kb or get_knowledge_base()
    if knowledge.are_nicknames(ka, kb_key):
        return True
    return len(min(ka, kb_key, key=len)) >= 3 and (ka.startswith(kb_key) or kb_key.startswith(ka))


def firm_key(firm: str | None) -> str:
    """Normalized firm key. "Fish & Richardson P.C." -> "fish richardson"."""
    if not firm:
        return ""
    s = strip_accents(firm).lower().replace("&", " and ")
    s = re.sub(r"\bp\.\s*c\.", " pc ", s)
    s = re.sub(r"\bl\.\s*l\.\s*p\.", " llp ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    toks = [t for t in s.split() if t not in _LEGAL_SUFFIX_TOKENS and t not in _FIRM_STOPWORDS]
    return " ".join(toks)


def canonical_firm(firm: str | None, kb: KnowledgeBase | None = None) -> str:
    key = firm_key(firm)
    if not key:
        return ""
    knowledge = kb or get_knowledge_base()
    return knowledge.resolve_firm_alias(key)


def firm_search_term(firm: str | None) -> str:
    """First distinctive word of the firm, for the site's CompanyName filter."""
    key = firm_key(firm)
    return key.split()[0] if key else ""


def metro_of(city: str | None, kb: KnowledgeBase | None = None) -> str:
    c = re.sub(r"\s+", " ", strip_accents(city or "").lower().strip())
    knowledge = kb or get_knowledge_base()
    return knowledge.get_metro(c) or c


def normalize_phone(raw: str | None) -> str | None:
    """US phone -> E.164. Returns None if not a valid 10-digit NANP number."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01" or digits[3] in "01":
        return None
    return f"+1{digits}"
