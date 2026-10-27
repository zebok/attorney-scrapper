"""Unit tests for the declarative KnowledgeBase."""

from txbar.rules import get_knowledge_base


def test_knowledge_base_singleton():
    kb1 = get_knowledge_base()
    kb2 = get_knowledge_base()
    assert kb1 is kb2


def test_knowledge_base_nicknames():
    kb = get_knowledge_base()
    assert kb.are_nicknames("Sam", "Samuel")
    assert kb.are_nicknames("Wayne", "Jack")
    assert kb.are_nicknames("Theodore", "Taylor")
    assert kb.are_nicknames("William", "Bill")
    assert not kb.are_nicknames("John", "Robert")


def test_knowledge_base_firm_aliases():
    kb = get_knowledge_base()
    assert kb.resolve_firm_alias("Ashurst Perkins Coie") == "perkins coie"
    assert kb.resolve_firm_alias("Perkins Coie") == "perkins coie"
    assert kb.are_firm_aliases("Perkins Coie", "Ashurst Perkins Coie")
    assert kb.are_firm_aliases("Wick Phillips", "Wick Phillips Gould Martin")


def test_knowledge_base_metro_clusters():
    kb = get_knowledge_base()
    assert kb.get_metro("Plano") == "dfw"
    assert kb.get_metro("Dallas") == "dfw"
    assert kb.same_metro("Dallas", "Plano")
    assert kb.same_metro("Austin", "Round Rock")
    assert not kb.same_metro("Houston", "Dallas")


def test_knowledge_base_domain_mappings():
    kb = get_knowledge_base()
    assert kb.get_firm_from_domain("jw.com") == "jackson walker"
    assert kb.get_firm_from_domain("ashurstperkins.com") == "perkins coie"
    assert kb.get_firm_from_domain("unknown-domain.com") is None
