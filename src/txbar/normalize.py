"""Name, email, firm and geography normalization.

Every trap found in the test sheet is handled here and covered by tests/test_normalize.py:
generational suffixes (III, IV), initials (W., J.W.), compound given/family names, hyphens,
apostrophes, nicknames (Sam, Rey, Brent, Jack), firm legal suffixes and firm mergers.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "esq"}

# Bidirectional nickname groups. Kept deliberately small and explicit: auditable > clever.
_NICKNAME_GROUPS = [
    {"samuel", "sam", "sammy"},
    {"reymundo", "raymundo", "rey"},
    {"brenton", "brent"},
    {"john", "jack", "johnny", "jon"},
    {"michael", "mike", "mick"},
    {"christopher", "chris"},
    {"theodore", "ted", "teddy", "theo"},
    {"katherine", "kathryn", "catherine", "kate", "katie", "kathy", "cathy"},
    {"matthew", "matt"},
    {"joshua", "josh"},
    {"gregory", "greg"},
    {"jeffrey", "jeff"},
    {"randall", "randy"},
    {"andrew", "andy", "drew"},
    {"nathaniel", "nathan", "nate"},
    {"william", "will", "bill", "billy", "liam"},
    {"robert", "rob", "bob", "bobby", "bert"},
    {"james", "jim", "jimmy", "jamie"},
    {"joseph", "joe", "joey"},
    {"daniel", "dan", "danny"},
    {"benjamin", "ben"},
    {"thomas", "tom", "tommy"},
    {"anthony", "tony"},
    {"elizabeth", "liz", "beth", "betsy", "eliza"},
    {"abigail", "abby", "abbie"},
    {"jennifer", "jen", "jenny"},
    {"virginia", "ginny"},
    {"alexander", "alexandra", "alex"},
    {"maxwell", "maximilian", "max"},
    {"zachary", "zackary", "zach", "zack", "zac"},
    {"jervonne", "jay"},
    {"patrick", "pat"},
    {"susan", "sue", "suzy"},
    {"mitchell", "mitch"},
    {"collin", "colin"},
]
NICKNAMES: dict[str, set[str]] = {}
for _group in _NICKNAME_GROUPS:
    for _n in _group:
        NICKNAMES.setdefault(_n, set()).update(_group)

# Firms that merged or rebranded. Canonical key -> aliases (normalized form).
FIRM_ALIASES: dict[str, set[str]] = {
    "perkins coie": {"ashurst perkins coie", "ashurstperkins", "perkins"},
    "ogletree deakins": {"ogletree deakins nash smoak stewart", "ogletree"},
    "norton rose fulbright": {"norton rose fulbright us", "fulbright jaworski"},
    "morgan lewis bockius": {"morgan lewis"},
    "skadden arps slate meagher flom": {"skadden"},
    "akin gump strauss hauer feld": {"akin gump", "akin"},
    "mehaffyweber": {"mehaffy weber"},
}

# Email domains that identify a firm better than its name does.
DOMAIN_FIRMS: dict[str, str] = {
    "ashurstperkins.com": "perkins coie",
    "jw.com": "jackson walker",
    "kslaw.com": "king spalding",
    "velaw.com": "vinson elkins",
    "gtlaw.com": "greenberg traurig",
    "lw.com": "latham watkins",
    "fr.com": "fish richardson",
    "hklaw.com": "holland knight",
    "stblaw.com": "simpson thacher bartlett",
}

_LEGAL_SUFFIX_TOKENS = {"llp", "pllc", "pc", "llc", "lp", "pa", "ltd", "inc", "plc", "p", "c", "l"}
_FIRM_STOPWORDS = {"the", "and", "of", "law", "firm", "group", "office", "offices", "attorneys", "at"}

METRO: dict[str, str] = {}
for _metro, _cities in {
    "dfw": ["dallas", "fort worth", "plano", "irving", "frisco", "arlington", "richardson", "addison",
            "mckinney", "grapevine", "southlake", "denton", "carrollton", "allen", "garland", "las colinas"],
    "houston": ["houston", "the woodlands", "sugar land", "katy", "pasadena", "spring", "pearland",
                "bellaire", "conroe", "humble", "league city", "galveston"],
    "austin": ["austin", "round rock", "cedar park", "georgetown", "san marcos", "pflugerville",
               "lakeway", "bee cave", "west lake hills"],
    "san antonio": ["san antonio", "new braunfels", "boerne"],
}.items():
    for _c in _cities:
        METRO[_c] = _metro


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
    first: str  # may be a single initial
    middle: str  # may be initials, may be empty
    last: str  # best guess family name (may be compound)
    suffix: str
    first_is_initial: bool
    last_variants: list[str] = field(default_factory=list)  # ordered search variants for the family name

    @property
    def first_key(self) -> str:
        return alpha_key(self.first)

    @property
    def last_key(self) -> str:
        return alpha_key(self.last)


def _is_initial(tok: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]\.?", tok))


def parse_input_name(raw: str, email: str | None = None) -> ParsedName:
    """Parse a free-form attorney name from the recruiting sheet.

    The email local-part is used to disambiguate compound names:
    "Luisa Gutierrez Quintero" + lgutierrezquintero@ -> family name "Gutierrez Quintero".
    """
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
    variants: list[str] = []
    if len(last_tokens) >= 2:
        compound = " ".join(last_tokens)
        tail = last_tokens[-1]
        head = last_tokens[0]
        # Prefer the compound family name when the email spells it out.
        if local and alpha_key(compound) in local:
            last = compound
            variants = [compound, tail, head]
        else:
            # "Mary Anna Rutledge": treat leading extra tokens as compound given/middle name.
            last = tail
            middle = " ".join([*(t for t in last_tokens[:-1]), middle]).strip()
            variants = [tail, compound]
    else:
        last = last_tokens[0]
        variants = [last]
        if "-" in last:  # "Rivera-Alvarado": the register may store either side or a space
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
    given_token: str  # e.g. "sam", "rey", "brent", "jack"-like full tokens before the family name
    initials: str  # e.g. "b" (bbuechele), "ml" (mlroberts), "js" (jscollins)
    pattern: str  # first.last | firstlast | initials+last | last_only | first_only | initials_only | unknown

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


def given_names_compatible(a: str, b: str) -> bool:
    """True when two given names are equal, nickname-equivalent, or one is an initial of the other."""
    ka, kb = alpha_key(a), alpha_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    if len(ka) == 1 or len(kb) == 1:
        return ka[0] == kb[0]
    if kb in NICKNAMES.get(ka, ()):  # sam <-> samuel
        return True
    # "Sarah-Michelle" vs "Sarah"; "Brent" prefix of "Brenton"
    return len(min(ka, kb, key=len)) >= 3 and (ka.startswith(kb) or kb.startswith(ka))


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


def canonical_firm(firm: str | None) -> str:
    key = firm_key(firm)
    for canon, aliases in FIRM_ALIASES.items():
        if key == canon or key in aliases:
            return canon
    return key


def firm_search_term(firm: str | None) -> str:
    """First distinctive word of the firm, for the site's CompanyName filter (prefix/contains match)."""
    key = firm_key(firm)
    return key.split()[0] if key else ""


def metro_of(city: str | None) -> str:
    c = re.sub(r"\s+", " ", strip_accents(city or "").lower().strip())
    return METRO.get(c, c)


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
