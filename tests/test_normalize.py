"""Unit tests for normalization rules and edge-case traps from the input sheet."""

from txbar.normalize import (
    canonical_firm,
    given_names_compatible,
    metro_of,
    normalize_phone,
    parse_email,
    parse_input_name,
)


def test_generational_suffixes():
    p1 = parse_input_name("Wayne Zeringue III", email="jzeringue@sidley.com")
    assert p1.last == "Zeringue"
    assert p1.suffix == "III"

    p2 = parse_input_name("Reymundo Rodriguez IV", email="rey.rodriguez@foley.com")
    assert p2.last == "Rodriguez"
    assert p2.suffix == "IV"


def test_initials_handling():
    # W. Buechele -> last is Buechele, first is W
    p = parse_input_name("W. Buechele", email="bbuechele@winstead.com")
    assert p.last == "Buechele"
    assert p.first == "W"
    assert p.first_is_initial is True

    # J.W. Beverly
    p2 = parse_input_name("J.W. Beverly", email="jb@fbfk.law")
    assert p2.last == "Beverly"
    assert p2.first == "J"
    assert p2.middle == "W"


def test_compound_and_hispanic_surnames():
    # Luisa Gutierrez Quintero with lgutierrezquintero@
    p = parse_input_name("Luisa Gutierrez Quintero", email="lgutierrezquintero@kslaw.com")
    assert p.last == "Gutierrez Quintero"
    assert p.first == "Luisa"

    # Mary Anna Rutledge (compound given name)
    p2 = parse_input_name("Mary Anna Rutledge", email="mrutledge@porterhedges.com")
    assert p2.last == "Rutledge"
    assert p2.first == "Mary"
    assert "Anna" in p2.middle

    # Hyphenated surname: Javier Rivera-Alvarado
    p3 = parse_input_name("Javier Rivera-Alvarado", email="javier.rivera-alvarado@stblaw.com")
    assert "Rivera" in p3.last_variants[0]


def test_apostrophes_and_hyphens():
    p = parse_input_name("Michael O'Brien")
    assert p.last == "O'Brien"
    assert p.last_key == "obrien"

    p2 = parse_input_name("Mitchell Alleluia-Feinberg")
    assert p2.last == "Alleluia-Feinberg"
    assert p2.last_key == "alleluiafeinberg"


def test_email_hints_disambiguation():
    # mlroberts@sidley.com with Name Michael Roberts -> middle initial 'l'
    p = parse_input_name("Michael Roberts", email="mlroberts@sidley.com")
    h = parse_email("mlroberts@sidley.com", p)
    assert h is not None
    assert h.first_initial == "m"
    assert h.middle_initial == "l"

    # jscollins@vorys.com with Name John Collins -> middle initial 's'
    p2 = parse_input_name("John Collins", email="jscollins@vorys.com")
    h2 = parse_email("jscollins@vorys.com", p2)
    assert h2 is not None
    assert h2.middle_initial == "s"


def test_nicknames_and_preferred_names():
    assert given_names_compatible("Sam", "Samuel") is True
    assert given_names_compatible("Rey", "Reymundo") is True
    assert given_names_compatible("Brent", "Brenton") is True
    assert given_names_compatible("Mike", "Michael") is True
    assert given_names_compatible("Jack", "John") is True


def test_firm_normalization_and_mergers():
    assert canonical_firm("Perkins Coie LLP") == "perkins coie"
    assert canonical_firm("Ashurst Perkins Coie") == "perkins coie"
    assert canonical_firm("Fish & Richardson P.C.") == "fish richardson"
    assert canonical_firm("Winstead PC") == "winstead"
    assert canonical_firm("Winstead P.C.") == "winstead"


def test_phone_normalization():
    assert normalize_phone("214-745-5214") == "+12147455214"
    assert normalize_phone("+214-745-5214") == "+12147455214"
    assert normalize_phone("+--") is None
    assert normalize_phone("") is None
    assert normalize_phone("123") is None  # invalid length


def test_metro_clustering():
    assert metro_of("Dallas") == "dfw"
    assert metro_of("Fort Worth") == "dfw"
    assert metro_of("Plano") == "dfw"
    assert metro_of("The Woodlands") == "houston"
    assert metro_of("Austin") == "austin"
