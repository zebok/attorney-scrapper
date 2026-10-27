"""HTML -> typed records for the Texas Bar directory.

Relies on the hCard microformat classes the site renders (given-name, additional-name, family-name,
honorific-suffix). Those are semantic and far more stable than layout or positional text.
Every parse validates structural "canary" markers; if they are missing we raise DomDriftError instead of
silently emitting empty rows.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from selectolax.lexbor import LexborHTMLParser, LexborNode

NO_RESULTS_MARKER = "Your search has returned no result"
RESULTS_MARKER = 'id="result-set"'
DETAIL_MARKERS = ("hcard-Lawyer-L-Name", "Bar Card Number")

_STATUS_BY_ICON = {
    "green": "Eligible to Practice in Texas",
    "red": "Not Eligible to Practice in Texas",
    "yellow": "Non-Practicing",
    "aqua": "Inactive",
    "blue": "Deceased",
}


class DomDriftError(RuntimeError):
    """The page no longer looks like what the parser was built for (layout change, block page, captcha)."""


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

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(d.pop("name"))
        d["full_name"] = self.name.full_name
        return d


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def _parse_name(h3: LexborNode | None) -> NameParts:
    out = NameParts()
    if h3 is None:
        return out
    given: list[str] = []
    additional: list[str] = []
    for span in h3.css("span"):
        cls = span.attributes.get("class") or ""
        text = _clean(span.text())
        if not text:
            continue
        if re.fullmatch(r"['\"‘’“”].*['\"‘’“”]", text):
            out.preferred_name = text.strip("'\"‘’“” ")
        elif "given-name" in cls:
            given.append(text)
        elif "additional-name" in cls:
            additional.append(text)
        elif "family-name" in cls:
            out.last_name = text
        elif "honorific-suffix" in cls:
            out.suffix = text
    # Search pages render the middle name as a 2nd given-name span; detail pages use additional-name.
    if given:
        out.first_name = given[0]
        additional = given[1:] + additional
    out.middle_name = " ".join(additional)
    return out


def _status(node: LexborNode) -> str:
    text_node = node.css_first(".status-text")
    if text_node is not None and _clean(text_node.text()):
        return _clean(text_node.text())
    icon = node.css_first(".status-icon")
    if icon is not None:
        for color, label in _STATUS_BY_ICON.items():
            if color in (icon.attributes.get("class") or ""):
                return label
    return ""


def _city_state_zip(address_text: str) -> tuple[str, str, str]:
    m = re.search(r"([A-Za-z .'\-]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$", address_text)
    if not m:
        return "", "", ""
    return _clean(m.group(1)), m.group(2), m.group(3)


def _address(node: LexborNode | None) -> str:
    if node is None:
        return ""
    html = node.html or ""
    lines = [_clean(re.sub(r"<[^>]+>", " ", part)) for part in re.split(r"<br\s*/?>", html, flags=re.I)]
    return ", ".join(line for line in lines if line)


def _phone(node: LexborNode) -> str:
    a = node.css_first('a[href^="tel:"]')
    return _clean(a.attributes.get("href", "").removeprefix("tel:")) if a is not None else ""


def _contact_id(node: LexborNode) -> str:
    for a in node.css("a[href]"):
        m = re.search(r"ContactID=(\d+)", a.attributes.get("href") or "", re.I)
        if m:
            return m.group(1)
    return ""


def _areas(text: str) -> list[str]:
    text = re.sub(r"(?i)practice areas:\s*", "", _clean(text))
    if not text or text.lower().startswith("none reported"):
        return []
    return [a.strip() for a in text.split(",") if a.strip()]


def parse_search_results(html: str) -> tuple[list[SearchHit], bool]:
    """Return (hits, has_more_pages). Raises DomDriftError if the page is not a results page."""
    if NO_RESULTS_MARKER in html:
        return [], False
    if RESULTS_MARKER not in html:
        raise DomDriftError("Results page markers not found (layout change or block page).")
    tree = LexborHTMLParser(html)
    hits: list[SearchHit] = []
    for art in tree.css("article.lawyer"):
        cid = _contact_id(art)
        if not cid:
            continue
        address = _address(art.css_first("p.address"))
        city, _, _ = _city_state_zip(address)
        firm_node = art.css_first("h5")
        areas_node = art.css_first("p.areas")
        hits.append(
            SearchHit(
                contact_id=cid,
                name=_parse_name(art.css_first("h3")),
                firm=_clean(firm_node.text()) if firm_node else "",
                city=city,
                address=address,
                phone=_phone(art),
                license_status=_status(art),
                practice_areas=_areas(areas_node.text()) if areas_node else [],
            )
        )
    if not hits:
        raise DomDriftError("Results page had no parsable lawyer cards and no 'no results' message.")
    has_more = bool(re.search(r"(?i)>\s*Next\s*<|name=\"ButtonName\"[^>]*value=\"Next", html)) and len(hits) >= 25
    return hits, has_more


def parse_profile(html: str, contact_id: str) -> Profile:
    if not all(m in html for m in DETAIL_MARKERS):
        raise DomDriftError(f"Profile {contact_id}: detail markers not found.")
    tree = LexborHTMLParser(html)
    vcard = tree.css_first("div.vcard")
    if vcard is None:
        raise DomDriftError(f"Profile {contact_id}: vcard block missing.")
    p = Profile(contact_id=contact_id, name=_parse_name(vcard.css_first("h3")))
    p.license_status = _status(vcard)
    firm_node = vcard.css_first("h5")
    p.firm = _clean(firm_node.text()) if firm_node else ""

    vtext = _clean(vcard.text(separator=" "))
    if m := re.search(r"Bar Card Number:\s*(\d+)", vtext):
        p.bar_card_number = m.group(1)
    if m := re.search(r"TX License Date:\s*(\d{2}/\d{2}/\d{4})", vtext):
        p.license_date = datetime.strptime(m.group(1), "%m/%d/%Y").date()
    if m := re.search(r"Primary Practice Location:\s*(.+?)(?:\s{2,}|$|\d{2,})", vtext):
        p.practice_location = _clean(m.group(1)).replace(" ,", ",")

    p.address = _address(vcard.css_first("p.address .value-block") or vcard.css_first("p.address"))
    p.city, p.state, p.zip = _city_state_zip(p.address)
    p.phone = _phone(tree.css_first("div.contact") or tree.root)

    for para in tree.css("div.practice-information p"):
        label_node = para.css_first("strong")
        if label_node is None:
            continue
        label = _clean(label_node.text()).split(":")[0].lower()
        value = _clean(para.text()).split(":", 1)[-1].strip()
        if value.lower().startswith(("none specified", "none reported")):
            value = ""
        if label == "firm" and value:
            p.firm = value
        elif label == "firm size":
            p.firm_size = value
        elif label == "occupation":
            p.occupation = value
        elif label == "practice areas":
            p.practice_areas = _areas(value)

    if not p.practice_areas and (areas := vcard.css_first("p.areas")):
        p.practice_areas = _areas(areas.text())

    first_td = tree.css_first("div.school tbody td")
    if first_td is not None:
        parts = [
            _clean(re.sub(r"<[^>]+>", " ", x))
            for x in re.split(r"<br\s*/?>", re.sub(r"(?s)<span class=\"show-mobile\">.*?</span>", "", first_td.html or ""))
        ]
        parts = [x for x in parts if x]
        p.law_school = parts[0] if parts else ""
        p.law_degree = parts[1] if len(parts) > 1 else ""
    if grad := tree.css_first("div.school tbody td.show-tablet"):
        if m := re.search(r"(\d{2})/(\d{4})", grad.text()):
            p.grad_year = int(m.group(2))

    if hist := tree.css_first("div.public-history p"):
        p.disciplinary_history = _clean(hist.text())
    return p
