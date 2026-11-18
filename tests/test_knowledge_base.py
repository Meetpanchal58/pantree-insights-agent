"""Knowledge-base lint: a wrong or one-sided entry would make the system confidently wrong, so entries are checked."""
from pantree_insights.catalog import value_columns
from pantree_insights.semantic import load_concepts, load_glossary, load_metrics

GLOSSARY = load_glossary()
VALUES = value_columns()


def test_every_alias_points_to_one_stored_value():
    seen: dict[str, str] = {}
    for value, aliases in GLOSSARY.value_aliases.items():
        assert value in VALUES, f"alias target '{value}' is not a stored value"
        for alias in aliases:
            assert alias.lower() not in seen, f"'{alias}' points to both {seen[alias.lower()]} and {value}"
            seen[alias.lower()] = value


def test_a_phrase_with_several_meanings_is_never_an_alias():
    aliases = {a.lower() for names in GLOSSARY.value_aliases.values() for a in names}
    for phrase, meanings in GLOSSARY.ambiguous.items():
        assert phrase.lower() not in aliases, f"'{phrase}' is ambiguous but also an alias of one value"
        assert len(meanings) >= 2, phrase


def test_metrics_and_concepts_load_and_agree():
    metrics, concepts = load_metrics(), load_concepts()
    assert all(m.source in metrics.sources for m in metrics.metrics.values())
    assert set(concepts.concepts) == set(concepts.concept_info)
